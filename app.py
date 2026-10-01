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
CONTAINER_PREFIX = "201"


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
    # 용기·환입을 고르면 출발지↔도착지가 화면에서 바뀌고, 단가는 바뀌기 전 도착지 기준으로 찾는다.
    places = sorted(set(dests["origin"].dropna()) | set(dests["dest"].dropna()))
    origin_idx = places.index(DEFAULT_ORIGIN) if DEFAULT_ORIGIN in places else None
    ss = st.session_state

    def on_dest_code():
        code = ss.get("dest_code", "").strip().split(".")[0]
        hit = dests[dests["code"] == code]
        if hit.empty:
            ss.code_msg = f"도착지 코드 {code} 를 찾을 수 없습니다."
            return
        o, d = hit.iloc[0]["origin"], hit.iloc[0]["dest"]
        ss.origin, ss.dest = (d, o) if ss.get("swapped") else (o, d)

    def on_type_change():
        want = ss.get("ptype") in SWAP_TYPES
        if want != ss.get("swapped", False):
            if ss.get("origin") and ss.get("dest"):
                ss.origin, ss.dest = ss.dest, ss.origin
            ss.swapped = want

    c1, c2, c3, c4, c5, c6 = st.columns([1.3, 1.3, 1, 1, 1, 1])
    origin = c1.selectbox("출발지", places, index=origin_idx, placeholder="선택", key="origin")
    dest = c2.selectbox("도착지", places, index=None, placeholder="선택 또는 입력", key="dest")
    c3.text_input("도착지 코드", key="dest_code", on_change=on_dest_code, placeholder="코드 입력 후 Enter")
    product_type = c4.selectbox("제품구분", PRODUCT_TYPES, key="ptype", on_change=on_type_change)
    unload_type = c5.selectbox("하차구분", UNLOAD_TYPES)
    empty_type = c6.selectbox("공차구분", EMPTY_TYPES)
    if msg := ss.pop("code_msg", None):
        st.warning(msg)

    swapped = ss.get("swapped", False)
    price_dest = origin if swapped else dest  # 단가는 원래(바뀌기 전) 도착지 기준
    if swapped and origin and dest:
        st.caption(f"{product_type}: 출발지와 도착지를 바꿨습니다. 단가는 {price_dest} 기준으로 적용합니다.")

    # 제품 입력
    st.subheader("제품")
    product_labels = [f"{r.product_code} - {r.product_name}" for r in products.itertuples()]
    if product_type == "용기":  # 용기는 201로 시작하는 코드를 먼저 보여 준다
        product_labels.sort(key=lambda l: (not l.startswith(CONTAINER_PREFIX), l))
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
        for k in ("slip_suffix", "origin", "dest", "dest_code", "ptype", "swapped", "paid"):
            st.session_state.pop(k, None)
        st.rerun()


# ───────────────────────── 전표 조회 ─────────────────────────
def _s(v):
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _pick(label, options, current, key):
    opts = list(options)
    if current and current not in opts:
        opts = [current] + opts
    idx = opts.index(current) if current in opts else None
    return st.selectbox(label, opts, index=idx, key=key)


def edit_invoice(serial, inv):
    """원본 인수증과 다를 때 전표 머리글과 제품 줄을 고쳐 저장한다."""
    k = f"edit_{serial}_"
    dests, partners = load_destinations(), load_partners()
    places = sorted(set(dests["origin"].dropna()) | set(dests["dest"].dropna()))

    c1, c2, c3 = st.columns(3)
    new_date = c1.date_input("작성일자", value=date.fromisoformat(inv.invoice_date),
                             format="YYYY-MM-DD", key=k + "date")
    new_slip = c2.text_input("전표번호", value=inv.slip_no or "", key=k + "slip")
    new_partner = _pick("거래처", partners["name"].tolist(), inv.partner_name, k + "partner")

    c1, c2, c3, c4, c5 = st.columns(5)
    new_origin = _pick("출발지", places, inv.origin, k + "origin")
    new_dest = _pick("도착지", places, inv.dest, k + "dest")
    new_ptype = _pick("제품구분", PRODUCT_TYPES, inv.product_type, k + "ptype")
    new_unload = _pick("하차구분", UNLOAD_TYPES, inv.unload_type, k + "unload")
    new_empty = _pick("공차구분", EMPTY_TYPES, inv.empty_type, k + "empty")

    old = q("""SELECT product_code, product_name, qty, unit_price
               FROM invoice_items WHERE serial_no = :s ORDER BY id""", s=serial)
    old_label = [f"{_s(r.product_code)} - {_s(r.product_name)}" for r in old.itertuples()]
    old_price = dict(zip(old_label, old["unit_price"]))
    options = [f"{r.product_code} - {r.product_name}" for r in load_products().itertuples()]
    options += [l for l in old_label if l not in options]

    st.caption("제품과 수량만 고칠 수 있습니다. 저장된 제품의 단가는 그대로 유지되고, 새로 추가한 제품만 단가표에서 자동 적용됩니다.")
    edited = st.data_editor(
        pd.DataFrame({"제품": old_label, "수량": old["qty"].astype("float64")}),
        num_rows="dynamic", hide_index=True, use_container_width=True, key=k + "items",
        column_config={
            "제품": st.column_config.SelectboxColumn("제품코드 - 제품명", options=options, width="large"),
            "수량": st.column_config.NumberColumn(min_value=0, step=1),
        },
    )

    # 용기·환입은 출발지/도착지가 바뀌어 저장되어 있으므로 단가는 출발지(원래 도착지) 기준
    price_dest = new_origin if new_ptype in SWAP_TYPES else new_dest
    prices = load_prices()
    rows, missing = [], []
    for r in edited.itertuples(index=False):
        label, qty = r[0], r[1]
        if not isinstance(label, str) or not label or pd.isna(qty) or qty == 0:
            continue
        code, name = label.split(" - ", 1)
        price = old_price.get(label)  # 이미 저장된 제품은 원래 단가를 절대 바꾸지 않음
        if price is None:  # 새로 추가한 제품만 단가표에서 찾음
            price = lookup_price(prices, name, price_dest, new_date) if price_dest else None
            if price is None:
                missing.append(name)
                price = 0.0
        rows.append({"product_code": code, "product_name": name, "qty": float(qty),
                     "unit_price": float(price), "fee": float(round(qty * price))})
    if rows:
        st.dataframe(pd.DataFrame(rows).rename(columns={
            "product_code": "제품코드", "product_name": "제품명", "qty": "수량",
            "unit_price": "단가", "fee": "운반비"}).style.format(
            {"수량": "{:,.0f}", "단가": "{:,.0f}", "운반비": "{:,.0f}"}),
            hide_index=True, use_container_width=True)
    if missing:
        st.warning(f"단가표에 없는 제품: {', '.join(missing)} ({price_dest}, {new_date} 기준) — 0원으로 계산됩니다.")

    fee_total = sum(x["fee"] for x in rows)
    vat = round(fee_total * VAT_RATE)
    total = fee_total + vat

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("운반비", won(fee_total), delta=won(fee_total - inv.fee_total) if fee_total != inv.fee_total else None)
    m2.metric("부가세", won(vat))
    m3.metric("합계", won(total))
    new_paid = m4.number_input("결제완료", min_value=0.0, value=float(inv.paid or 0), step=10000.0, key=k + "paid")

    b1, b2 = st.columns([3, 1])
    if b1.button("✏️ 수정 내용 저장", type="primary", use_container_width=True, key=k + "save"):
        if not rows:
            st.error("제품 줄이 하나 이상 있어야 합니다. 전표를 없애려면 삭제를 이용하세요.")
            return
        biz = partners.loc[partners["name"] == new_partner, "biz_no"]
        with engine().begin() as conn:
            conn.execute(text("""UPDATE invoices SET invoice_date=:d, slip_no=:slip, partner_name=:pn,
                                    biz_no=:bn, origin=:o, dest=:de, product_type=:pt, unload_type=:ut,
                                    empty_type=:et, fee_total=:f, vat=:v, total=:t, paid=:p
                                 WHERE serial_no=:s"""), {
                "d": new_date.isoformat(), "slip": new_slip.strip(), "pn": new_partner,
                "bn": biz.iloc[0] if not biz.empty else inv.biz_no, "o": new_origin, "de": new_dest,
                "pt": new_ptype, "ut": new_unload, "et": new_empty,
                "f": fee_total, "v": vat, "t": total, "p": float(new_paid), "s": serial})
            conn.execute(text("DELETE FROM invoice_items WHERE serial_no = :s"), {"s": serial})
            conn.execute(text("""INSERT INTO invoice_items (serial_no, product_code, product_name, qty, unit_price, fee)
                                 VALUES (:serial_no, :product_code, :product_name, :qty, :unit_price, :fee)"""),
                         [{**x, "serial_no": serial} for x in rows])
        st.session_state.flash = f"{serial} 수정 완료"
        st.rerun()

    with b2.popover("🗑️ 삭제", use_container_width=True):
        st.write(f"{serial} 전표를 완전히 삭제할까요?")
        if st.button("삭제 확인", type="primary", key=k + "del"):
            with engine().begin() as conn:
                conn.execute(text("DELETE FROM invoice_items WHERE serial_no = :s"), {"s": serial})
                conn.execute(text("DELETE FROM invoices WHERE serial_no = :s"), {"s": serial})
            st.session_state.flash = f"{serial} 삭제 완료"
            st.rerun()


def page_list():
    st.header("전표 조회")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)
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

    # 상세 / 수정 / 삭제
    st.subheader("전표 상세 · 수정")
    serial = st.selectbox("일련번호", df.serial_no.tolist(), index=None,
                          placeholder="수정하거나 볼 전표를 고르세요")
    if serial:
        edit_invoice(serial, df[df.serial_no == serial].iloc[0])

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


# ───────────────────────── 단가 변경 (유가 연동) ─────────────────────────
FUEL_FACTOR = 0.45      # 단가변동율 = (변동유가 - 기준유가) / 기준유가 × 45%
FUEL_THRESHOLD = 100    # 기준유가와 100원 이상 차이 나는 분기가 나오면 변경


@st.cache_resource
def ensure_fuel_table():
    with engine().begin() as conn:
        conn.execute(text("""CREATE TABLE IF NOT EXISTS fuel_prices (
                                 year INTEGER NOT NULL, quarter INTEGER NOT NULL,
                                 price DOUBLE PRECISION NOT NULL,
                                 PRIMARY KEY (year, quarter))"""))
    return True


def quarter_of(d: date):
    return d.year, (d.month - 1) // 3 + 1


def prev_quarter(y, qt):
    return (y, qt - 1) if qt > 1 else (y - 1, 4)


def next_quarter(y, qt):
    return (y, qt + 1) if qt < 4 else (y + 1, 1)


def quarter_start(y, qt):
    return date(y, 3 * (qt - 1) + 1, 1)


def qlabel(y, qt):
    m = 3 * (qt - 1) + 1
    return f"{y}년 {qt}분기({m}~{m + 2}월)"


def round_price(x, mode):
    from decimal import Decimal, ROUND_HALF_UP
    unit = {"원 단위": Decimal("1"), "10원 단위": Decimal("10"), "소수 첫째 자리": Decimal("0.1")}[mode]
    return float((Decimal(str(x)) / unit).quantize(Decimal("1"), ROUND_HALF_UP) * unit)


def page_price_change():
    st.header("단가 변경 (유가 연동)")
    ensure_fuel_table()
    if msg := st.session_state.pop("flash", None):
        st.success(msg)

    # 1) 분기별 경유 평균가
    st.subheader("① 분기별 자동차용 경유 평균가")
    st.caption("오피넷(www.opinet.co.kr) → 유가통계에서 분기별 자동차용 경유 평균가를 확인해 입력하세요. "
               "엑셀에서 복사해 표에 붙여넣기(Ctrl+V)도 됩니다.")
    fuel = q("SELECT year AS 연도, quarter AS 분기, price AS 경유평균가 FROM fuel_prices ORDER BY year, quarter")
    fuel = fuel.astype({"연도": "Int64", "분기": "Int64", "경유평균가": "float64"})
    fuel_edit = st.data_editor(
        fuel, num_rows="dynamic", hide_index=True, key="fuel_editor",
        column_config={
            "연도": st.column_config.NumberColumn(min_value=2000, max_value=2100, step=1, format="%d"),
            "분기": st.column_config.NumberColumn(min_value=1, max_value=4, step=1, format="%d"),
            "경유평균가": st.column_config.NumberColumn(min_value=0, format="%.2f"),
        },
    )
    if st.button("유가 저장"):
        clean = fuel_edit.dropna()
        if clean.duplicated(["연도", "분기"]).any():
            st.error("같은 연도·분기가 두 번 입력되어 있습니다.")
        else:
            with engine().begin() as conn:
                conn.execute(text("DELETE FROM fuel_prices"))
                if not clean.empty:
                    conn.execute(text("INSERT INTO fuel_prices (year, quarter, price) VALUES (:y, :q, :p)"),
                                 [{"y": int(r.연도), "q": int(r.분기), "p": float(r.경유평균가)}
                                  for r in clean.itertuples(index=False)])
            st.session_state.flash = "유가를 저장했습니다."
            st.rerun()

    fuel_map = {(int(r.연도), int(r.분기)): float(r.경유평균가) for r in fuel.dropna().itertuples(index=False)}

    # 2) 변경 판단
    st.subheader("② 단가 변경 판단")
    prices = q("SELECT apply_date, product_code, product_name, dest, price FROM unit_prices")
    if prices.empty:
        st.info("단가표가 비어 있습니다.")
        return
    last_apply = date.fromisoformat(prices["apply_date"].max())
    base_q = prev_quarter(*quarter_of(last_apply))
    st.write(f"현재 단가의 최근 적용일: **{last_apply}**  →  기준유가 분기: **{qlabel(*base_q)}**")
    if base_q not in fuel_map:
        st.warning(f"{qlabel(*base_q)} 경유 평균가가 없습니다. 위 표에 먼저 입력하세요.")
        return
    base = fuel_map[base_q]

    rows, trigger = [], None
    cur = next_quarter(*base_q)
    while cur in fuel_map:
        diff = fuel_map[cur] - base
        hit = abs(diff) >= FUEL_THRESHOLD
        rows.append({"분기": qlabel(*cur), "경유평균가": fuel_map[cur], "기준유가 대비": diff,
                     "판정": "변경" if hit else "100원 미만"})
        if hit:
            trigger = cur
            break
        cur = next_quarter(*cur)

    st.write(f"기준유가: **{base:,.2f}원**")
    if rows:
        st.dataframe(pd.DataFrame(rows).style.format({"경유평균가": "{:,.2f}", "기준유가 대비": "{:+,.2f}"}),
                     hide_index=True, use_container_width=True)
    if not trigger:
        st.info(f"기준유가와 {FUEL_THRESHOLD}원 이상 차이 나는 분기가 아직 없습니다. 단가 변경 대상이 아닙니다.")
        return

    new_fuel = fuel_map[trigger]
    rate = (new_fuel - base) / base * FUEL_FACTOR
    apply_date = quarter_start(*next_quarter(*trigger))
    st.success(f"{qlabel(*trigger)} 경유가 {new_fuel:,.2f}원 → 단가변동율 "
               f"({new_fuel:,.2f} − {base:,.2f}) ÷ {base:,.2f} × 45 = **{rate * 100:+.3f}%**, "
               f"적용일자 **{apply_date}**")

    # 3) 새 단가 미리보기
    st.subheader("③ 새 단가 미리보기")
    mode = st.radio("단가 반올림", ["원 단위", "10원 단위", "소수 첫째 자리"], horizontal=True)
    latest = (prices[prices["apply_date"] < apply_date.isoformat()]
              .sort_values("apply_date")
              .groupby(["product_name", "dest"], as_index=False).last())
    latest["새단가"] = [round_price(p * (1 + rate), mode) for p in latest["price"]]
    latest["차이"] = latest["새단가"] - latest["price"]
    view = latest.rename(columns={"product_code": "제품코드", "product_name": "제품명", "dest": "도착지",
                                  "apply_date": "기존 적용일", "price": "현재단가"})[
        ["제품코드", "제품명", "도착지", "기존 적용일", "현재단가", "새단가", "차이"]]
    st.dataframe(view.style.format({"현재단가": "{:,.2f}", "새단가": "{:,.2f}", "차이": "{:+,.2f}"}),
                 hide_index=True, use_container_width=True)

    exists = int(q("SELECT COUNT(*) AS n FROM unit_prices WHERE apply_date = :d",
                   d=apply_date.isoformat()).n[0])
    if exists:
        st.warning(f"{apply_date} 적용 단가가 이미 {exists}건 있습니다. 중복 저장을 막기 위해 저장 버튼을 숨겼습니다.")
        return
    if st.button(f"💾 새 단가 {len(view)}건 저장 (적용일 {apply_date})", type="primary"):
        with engine().begin() as conn:
            conn.execute(text("""INSERT INTO unit_prices (apply_date, product_code, product_name, dest, price)
                                 VALUES (:a, :c, :n, :d, :p)"""),
                         [{"a": apply_date.isoformat(), "c": r.product_code, "n": r.product_name,
                           "d": r.dest, "p": float(r.새단가)} for r in latest.itertuples(index=False)])
        clear_caches()
        st.session_state.flash = f"{apply_date} 적용 새 단가 {len(view)}건을 저장했습니다."
        st.rerun()


# ───────────────────────── main ─────────────────────────
if check_password():
    page = st.sidebar.radio("메뉴", ["전표 입력", "전표 조회", "단가 변경", "단가·거래처 관리"])
    if st.sidebar.button("🔄 기준정보 새로고침"):
        clear_caches()
    {"전표 입력": page_entry, "전표 조회": page_list, "단가 변경": page_price_change,
     "단가·거래처 관리": page_admin}[page]()
