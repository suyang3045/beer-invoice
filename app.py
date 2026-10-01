"""맥주 운송전표 입력/조회 웹앱 (Streamlit).

로컬 실행:  streamlit run app.py   (DB_URL이 없으면 local_test.db SQLite 사용)
배포:       Streamlit Community Cloud + Supabase(PostgreSQL)
"""
import hmac
import io
import os
from datetime import date, datetime

import pandas as pd
import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import DEFAULT_PARTNER, make_engine

st.set_page_config(page_title="맥주 운송전표", page_icon="🍺", layout="wide")

PRODUCT_TYPES = ["제품", "용기", "자재", "일반", "환입"]
SWAP_TYPES = {"용기", "환입"}  # 출발지/도착지를 바꿔 저장하고, 단가는 원래 도착지 기준
UNLOAD_TYPES = ["당일착", "익일착"]
EMPTY_TYPES = ["상차", "공차"]
VAT_RATE = 0.1
EMPTY_ROWS = 5
DEFAULT_ORIGIN = "강원공장"


# ───────────────────────── 공통 ─────────────────────────
def secret(key, default=None):
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, default)


@st.cache_resource
def engine():
    return make_engine(secret("DB_URL", "sqlite:///local_test.db"))


def q(sql, **params):
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


@st.cache_data(ttl=600)
def load_destinations():
    return q("SELECT code, origin, dest FROM destinations ORDER BY origin, dest")


@st.cache_data(ttl=600)
def load_products():
    return q("""SELECT DISTINCT product_code, product_name FROM unit_prices
                WHERE product_code IS NOT NULL AND product_code <> ''
                ORDER BY product_code""")


@st.cache_data(ttl=600)
def load_prices():
    return q("SELECT apply_date, product_name, dest, price FROM unit_prices")


@st.cache_data(ttl=60)
def load_partners():
    return q("SELECT name, biz_no FROM partners ORDER BY name")


def clear_caches():
    load_destinations.clear()
    load_products.clear()
    load_prices.clear()
    load_partners.clear()


def lookup_price(prices, name, dest, d: date):
    """적용일자 <= 작성일자 중 가장 최근 단가."""
    m = prices[(prices.product_name == name) & (prices.dest == dest)
               & (prices.apply_date <= d.isoformat())]
    if m.empty:
        return None
    return float(m.sort_values("apply_date").iloc[-1]["price"])


def previous_unpaid(partner_name, d: date):
    df = q("""SELECT COALESCE(SUM(total), 0) AS t, COALESCE(SUM(paid), 0) AS p
              FROM invoices WHERE partner_name = :n AND invoice_date <= :d""",
           n=partner_name, d=d.isoformat())
    return max(0.0, float(df.t[0]) - float(df.p[0]))


def next_serial(conn, d: date):
    prefix = f"S{d:%y%m}"
    row = conn.execute(
        text("SELECT serial_no FROM invoices WHERE serial_no LIKE :p ORDER BY serial_no DESC LIMIT 1"),
        {"p": prefix + "-%"},
    ).fetchone()
    seq = int(row[0].split("-")[1]) + 1 if row else 1
    return f"{prefix}-{seq:04d}"


def won(x):
    return f"{x:,.0f}"


def check_password():
    pw = secret("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return True
    st.title("🍺 맥주 운송전표")
    with st.form("login"):
        entered = st.text_input("비밀번호", type="password")
        if st.form_submit_button("로그인"):
            if hmac.compare_digest(entered, str(pw)):
                st.session_state.authed = True
                st.rerun()
            st.error("비밀번호가 맞지 않습니다.")
    return False


# ───────────────────────── 전표 입력 ─────────────────────────
def page_entry():
    st.header("운송전표 입력")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)

    dests, products, prices, partners = (load_destinations(), load_products(),
                                         load_prices(), load_partners())
    if dests.empty or products.empty:
        st.warning("목적지나 단가 정보가 없습니다. migrate.py로 기존 데이터를 옮기거나 "
                   "'단가·거래처 관리' 메뉴에서 입력하세요.")

    # 거래처
    labels = [f"{r.name} ({r.biz_no})" for r in partners.itertuples()]
    default_idx = next((i for i, l in enumerate(labels) if DEFAULT_PARTNER[0] in l), 0)
    sel = st.selectbox("거래처 (입력해서 검색)", labels, index=default_idx)
    partner = partners.iloc[labels.index(sel)]

    # 기본 정보
    c1, c2 = st.columns([1, 2])
    inv_date = c1.date_input("작성일자", value=date.today(), format="YYYY-MM-DD")
    slip_suffix = c2.text_input(f"전표번호  ({inv_date:%Y%m}_ 뒤)", key="slip_suffix")

    # 출발지 / 도착지 / 도착지 코드 (기존 프로그램과 같은 방식)
    origins = sorted(dests["origin"].dropna().unique().tolist())
    dest_names = sorted(dests["dest"].dropna().unique().tolist())
    origin_idx = origins.index(DEFAULT_ORIGIN) if DEFAULT_ORIGIN in origins else None

    def on_dest_code():
        code = st.session_state.get("dest_code", "").strip().split(".")[0]
        hit = dests[dests["code"] == code]
        if hit.empty:
            st.session_state.code_msg = f"도착지 코드 {code} 를 찾을 수 없습니다."
        else:
            st.session_state.origin = hit.iloc[0]["origin"]
            st.session_state.dest = hit.iloc[0]["dest"]

    c1, c2, c3, c4, c5, c6 = st.columns([1.3, 1.3, 1, 1, 1, 1])
    origin = c1.selectbox("출발지", origins, index=origin_idx, placeholder="선택", key="origin")
    dest = c2.selectbox("도착지", dest_names, index=None, placeholder="선택 또는 입력", key="dest")
    c3.text_input("도착지 코드", key="dest_code", on_change=on_dest_code, placeholder="코드 입력 후 Enter")
    product_type = c4.selectbox("제품구분", PRODUCT_TYPES)
    unload_type = c5.selectbox("하차구분", UNLOAD_TYPES)
    empty_type = c6.selectbox("공차구분", EMPTY_TYPES)
    if msg := st.session_state.pop("code_msg", None):
        st.warning(msg)

    price_dest = dest  # 단가는 선택한 도착지 기준
    if origin and dest and product_type in SWAP_TYPES:
        origin, dest = dest, origin
        st.caption(f"{product_type}: {origin} → {dest} 로 저장하고, 단가는 {price_dest} 기준으로 적용합니다.")

    # 제품 입력
    st.subheader("제품")
    product_labels = [f"{r.product_code} - {r.product_name}" for r in products.itertuples()]
    ver = st.session_state.setdefault("editor_ver", 0)
    edited = st.data_editor(
        pd.DataFrame({"제품": pd.Series([""] * EMPTY_ROWS, dtype="object"),
                      "수량": pd.Series([float("nan")] * EMPTY_ROWS, dtype="float64")}),
        column_config={
            "제품": st.column_config.SelectboxColumn("제품코드 - 제품명", options=product_labels, width="large"),
            "수량": st.column_config.NumberColumn("수량", min_value=0, step=1),
        },
        num_rows="dynamic", use_container_width=True, hide_index=True, key=f"items_{ver}",
    )

    items, missing = [], []
    for row in edited.itertuples(index=False):
        label, qty = row[0], row[1]
        if not isinstance(label, str) or not label or pd.isna(qty) or qty == 0:
            continue
        code, name = label.split(" - ", 1)
        price = lookup_price(prices, name, price_dest, inv_date) if price_dest else None
        if price is None:
            missing.append(name)
            price = 0.0
        items.append({"product_code": code, "product_name": name, "qty": float(qty),
                      "unit_price": price, "fee": float(round(qty * price))})

    if items:
        view = pd.DataFrame(items).rename(columns={
            "product_code": "제품코드", "product_name": "제품명", "qty": "수량",
            "unit_price": "단가", "fee": "운반비"})
        st.dataframe(view.style.format({"수량": "{:,.0f}", "단가": "{:,.0f}", "운반비": "{:,.0f}"}),
                     use_container_width=True, hide_index=True)
    if missing:
        st.warning(f"단가가 없는 제품: {', '.join(missing)} (도착지 {price_dest}, {inv_date} 기준) — 0원으로 계산됩니다.")

    # 금액 / 미수금
    fee_total = sum(i["fee"] for i in items)
    vat = round(fee_total * VAT_RATE)
    total = fee_total + vat
    prev = previous_unpaid(partner["name"], inv_date)

    st.subheader("금액")
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("운반비", won(fee_total))
    m2.metric("부가세", won(vat))
    m3.metric("합계", won(total))
    m4.metric("이전 미결제", won(prev))
    paid = m5.number_input("결제완료", min_value=0, step=10000, key="paid")
    m6.metric("미결제액", won(max(0, prev + total - paid)))

    if st.button("💾 저장", type="primary", use_container_width=True):
        if not origin or not dest:
            st.error("출발지와 도착지를 선택하세요.")
            return
        if not items:
            st.error("제품과 수량을 한 줄 이상 입력하세요.")
            return
        for _ in range(3):  # 동시에 두 명이 저장해 일련번호가 겹치면 다시 시도
            try:
                with engine().begin() as conn:
                    serial = next_serial(conn, inv_date)
                    conn.execute(text("""
                        INSERT INTO invoices (serial_no, invoice_date, slip_no, partner_name, biz_no,
                            origin, dest, product_type, unload_type, empty_type,
                            fee_total, vat, total, paid, created_at)
                        VALUES (:serial_no, :invoice_date, :slip_no, :partner_name, :biz_no,
                            :origin, :dest, :product_type, :unload_type, :empty_type,
                            :fee_total, :vat, :total, :paid, :created_at)"""), {
                        "serial_no": serial, "invoice_date": inv_date.isoformat(),
                        "slip_no": f"{inv_date:%Y%m}_{slip_suffix.strip()}",
                        "partner_name": partner["name"], "biz_no": partner["biz_no"],
                        "origin": origin, "dest": dest, "product_type": product_type,
                        "unload_type": unload_type, "empty_type": empty_type,
                        "fee_total": fee_total, "vat": vat, "total": total, "paid": float(paid),
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    conn.execute(text("""
                        INSERT INTO invoice_items (serial_no, product_code, product_name, qty, unit_price, fee)
                        VALUES (:serial_no, :product_code, :product_name, :qty, :unit_price, :fee)"""),
                        [{**i, "serial_no": serial} for i in items])
                break
            except IntegrityError:
                continue
        else:
            st.error("일련번호 생성에 실패했습니다. 다시 저장해 주세요.")
            return

        st.session_state.flash = f"{serial} 저장 완료 (합계 {won(total)}원)"
        st.session_state.editor_ver += 1
        for k in ("slip_suffix", "dest", "dest_code", "paid"):
            st.session_state.pop(k, None)
        st.rerun()


# ───────────────────────── 전표 조회 ─────────────────────────
def page_list():
    st.header("전표 조회")
    partners = load_partners()
    c1, c2, c3 = st.columns(3)
    today = date.today()
    d_from = c1.date_input("시작일", value=today.replace(day=1), format="YYYY-MM-DD")
    d_to = c2.date_input("종료일", value=today, format="YYYY-MM-DD")
    who = c3.selectbox("거래처", ["전체"] + partners["name"].tolist())

    where = "v.invoice_date BETWEEN :a AND :b"
    params = {"a": d_from.isoformat(), "b": d_to.isoformat()}
    if who != "전체":
        where += " AND v.partner_name = :n"
        params["n"] = who
    df = q(f"SELECT v.* FROM invoices v WHERE {where} ORDER BY v.invoice_date, v.serial_no", **params)

    if df.empty:
        st.info("이 기간에 저장된 전표가 없습니다.")
        return

    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("전표 수", f"{len(df)}건")
    s2.metric("운반비", won(df.fee_total.sum()))
    s3.metric("합계(부가세 포함)", won(df.total.sum()))
    s4.metric("결제완료", won(df.paid.sum()))
    s5.metric("미결제", won(df.total.sum() - df.paid.sum()))

    view = df[["serial_no", "invoice_date", "slip_no", "partner_name", "origin", "dest",
               "product_type", "unload_type", "empty_type", "fee_total", "vat", "total", "paid"]]
    names = {"serial_no": "일련번호", "invoice_date": "작성일자", "slip_no": "전표번호",
             "partner_name": "상호", "origin": "출발지", "dest": "도착지", "product_type": "제품구분",
             "unload_type": "하차구분", "empty_type": "공차구분", "fee_total": "운반비",
             "vat": "부가세", "total": "합계", "paid": "결제완료"}
    st.caption("결제완료 칸만 수정할 수 있습니다. 수정 후 아래 버튼을 누르세요.")
    edited = st.data_editor(
        view.rename(columns=names), hide_index=True, use_container_width=True,
        disabled=[v for k, v in names.items() if k != "paid"],
        column_config={c: st.column_config.NumberColumn(format="localized")
                       for c in ("운반비", "부가세", "합계", "결제완료")},
        key="list_editor",
    )
    edited["결제완료"] = edited["결제완료"].fillna(0)
    changed = edited[edited["결제완료"] != view["paid"].values]
    if st.button(f"결제완료 변경 저장 ({len(changed)}건)", disabled=changed.empty):
        with engine().begin() as conn:
            for r in changed.itertuples(index=False):
                conn.execute(text("UPDATE invoices SET paid = :p WHERE serial_no = :s"),
                             {"p": float(r.결제완료), "s": r.일련번호})
        st.success("저장했습니다.")
        st.rerun()

    # 상세 / 삭제
    st.subheader("전표 상세")
    serial = st.selectbox("일련번호", df.serial_no.tolist(), index=None)
    if serial:
        items = q("""SELECT product_code AS 제품코드, product_name AS 제품명, qty AS 수량,
                            unit_price AS 단가, fee AS 운반비
                     FROM invoice_items WHERE serial_no = :s ORDER BY id""", s=serial)
        st.dataframe(items, hide_index=True, use_container_width=True)
        if st.checkbox(f"{serial} 삭제") and st.button("삭제 확인", type="secondary"):
            with engine().begin() as conn:
                conn.execute(text("DELETE FROM invoice_items WHERE serial_no = :s"), {"s": serial})
                conn.execute(text("DELETE FROM invoices WHERE serial_no = :s"), {"s": serial})
            st.rerun()

    # 엑셀 다운로드
    all_items = q(f"""SELECT i.* FROM invoice_items i JOIN invoices v ON v.serial_no = i.serial_no
                      WHERE {where} ORDER BY i.serial_no, i.id""", **params)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        view.rename(columns=names).to_excel(xw, sheet_name="전표", index=False)
        all_items.drop(columns=["id"]).rename(columns={
            "serial_no": "일련번호", "product_code": "제품코드", "product_name": "제품명",
            "qty": "수량", "unit_price": "단가", "fee": "운반비"}).to_excel(xw, sheet_name="제품내역", index=False)
    st.download_button("📥 엑셀로 내려받기", buf.getvalue(),
                       file_name=f"운송전표_{d_from}_{d_to}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ───────────────────────── 기준정보 관리 ─────────────────────────
def page_admin():
    st.header("단가·거래처 관리")
    t1, t2, t3 = st.tabs(["거래처", "목적지", "단가"])

    with t1, st.form("partner_form", clear_on_submit=True):
        n = st.text_input("상호")
        b = st.text_input("사업자번호")
        if st.form_submit_button("거래처 추가"):
            if not n.strip() or not b.strip():
                st.error("상호와 사업자번호를 모두 입력하세요.")
            else:
                try:
                    with engine().begin() as conn:
                        conn.execute(text("INSERT INTO partners (biz_no, name) VALUES (:b, :n)"),
                                     {"b": b.strip(), "n": n.strip()})
                    load_partners.clear()
                    st.success(f"{n} 등록 완료")
                except IntegrityError:
                    st.error("이미 등록된 사업자번호입니다.")
    with t1:
        st.dataframe(load_partners(), hide_index=True, use_container_width=True)

    with t2, st.form("dest_form", clear_on_submit=True):
        a, b_, c = st.columns(3)
        code = a.text_input("코드")
        org = b_.text_input("출발지")
        dst = c.text_input("도착지")
        if st.form_submit_button("목적지 추가"):
            try:
                with engine().begin() as conn:
                    conn.execute(text("INSERT INTO destinations (code, origin, dest) VALUES (:c, :o, :d)"),
                                 {"c": code.strip(), "o": org.strip(), "d": dst.strip()})
                load_destinations.clear()
                st.success("등록 완료")
            except IntegrityError:
                st.error("이미 있는 코드입니다.")
    with t2:
        st.dataframe(load_destinations(), hide_index=True, use_container_width=True)

    with t3:
        st.markdown("**엑셀/CSV 일괄 등록** — 열 이름: 적용일자, 제품코드, 제품명, 도착지, 단가")
        up = st.file_uploader("파일 선택", type=["xlsx", "csv"])
        if up is not None:
            new = pd.read_csv(up) if up.name.endswith(".csv") else pd.read_excel(up)
            need = ["적용일자", "제품코드", "제품명", "도착지", "단가"]
            if not set(need) <= set(new.columns):
                st.error(f"필요한 열: {', '.join(need)}")
            else:
                new = new[need].dropna(subset=["적용일자", "제품명", "도착지", "단가"])
                new["적용일자"] = pd.to_datetime(new["적용일자"]).dt.strftime("%Y-%m-%d")
                new["제품코드"] = new["제품코드"].astype(str).str.replace(r"\.0$", "", regex=True)
                st.dataframe(new, hide_index=True, use_container_width=True)
                if st.button(f"{len(new)}건 등록"):
                    with engine().begin() as conn:
                        conn.execute(text("""INSERT INTO unit_prices
                                (apply_date, product_code, product_name, dest, price)
                                VALUES (:a, :c, :n, :d, :p)"""),
                            [{"a": r.적용일자, "c": r.제품코드, "n": r.제품명, "d": r.도착지,
                              "p": float(r.단가)} for r in new.itertuples()])
                    clear_caches()
                    st.success("등록 완료")
        st.markdown("**현재 단가표**")
        st.dataframe(q("""SELECT apply_date AS 적용일자, product_code AS 제품코드,
                                 product_name AS 제품명, dest AS 도착지, price AS 단가
                          FROM unit_prices ORDER BY product_code, dest, apply_date DESC"""),
                     hide_index=True, use_container_width=True)


# ───────────────────────── main ─────────────────────────
if check_password():
    page = st.sidebar.radio("메뉴", ["전표 입력", "전표 조회", "단가·거래처 관리"])
    if st.sidebar.button("🔄 기준정보 새로고침"):
        clear_caches()
    {"전표 입력": page_entry, "전표 조회": page_list, "단가·거래처 관리": page_admin}[page]()
