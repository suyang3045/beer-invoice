"""맥주 운송전표 입력/조회 웹앱 (Streamlit).

로컬 실행:  streamlit run app.py   (DB_URL이 없으면 local_test.db SQLite 사용)
배포:       Streamlit Community Cloud + Supabase(PostgreSQL)
"""
import hashlib
import hmac
import re
import secrets as pysecrets
import io
import os
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError

from db import DEFAULT_PARTNER, make_engine

st.set_page_config(page_title="맥주 운송전표", page_icon="🍺", layout="wide", initial_sidebar_state="collapsed")

# 앱 안쪽의 Streamlit 메뉴·장식 숨기기 (사이드바 열기 버튼은 남겨 둠)
st.markdown("""
<style>
#MainMenu, [data-testid="stToolbar"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"], footer,
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stExpandSidebarButton"], [data-testid="stHeader"], header {display: none !important;}
.block-container, [data-testid="stMainBlockContainer"] {padding-top: 1rem !important;}
</style>
""", unsafe_allow_html=True)

PRODUCT_TYPES = ["제품", "용기", "자재", "일반", "환입"]
SWAP_TYPES = {"용기", "환입"}  # 출발지/도착지를 바꿔 저장하고, 단가는 원래 도착지 기준
UNLOAD_TYPES = ["당일착", "익일착"]
EMPTY_TYPES = ["상차", "공차"]
VAT_RATE = 0.1
EMPTY_ROWS = 5
DEFAULT_ORIGIN = "강원공장"
CONTAINER_PREFIX = "201"
MAX_MATCHES = 30


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
    return q("SELECT apply_date, product_code, product_name, dest, price FROM unit_prices")


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


# 검색어 별칭: 현장에서 부르는 말 → 제품명에 실제로 들어 있는 글자
SEARCH_ALIASES = {
    "생맥주": ["유흥생"], "생맥": ["유흥생"], "생": ["유흥생"], "케그": ["유흥생"],
    "캘리": ["캘리", "켈리"], "켈리": ["캘리", "켈리"],
    "하이트": ["하이트", "hite"], "맥스": ["맥스", "맥)"],
    "필라이트": ["필)", "필후)", "필클리어"],
    "페트": ["피쳐"], "피처": ["피쳐"],
    "파렛트": ["파렛", "pallet"], "팔레트": ["파렛", "pallet"], "파레트": ["파렛", "pallet"],
    "박스": ["box"], "가스": ["co2"], "탄산": ["co2"],
}


def search_products(labels, kw):
    """띄어 쓴 단어가 모두 들어 있는 제품 (대소문자·공백 무시, 코드·별칭으로도 검색)."""
    words = [w for w in kw.lower().split() if w]
    alts = [[a.lower().replace(" ", "") for a in SEARCH_ALIASES.get(w, [])] + [w] for w in words]
    return [l for l in labels
            if all(any(a in l.lower().replace(" ", "") for a in alt) for alt in alts)]


def current_product_labels(prices, d: date):
    """작성일자 기준 가장 최근 적용일자의 단가표에 있는 제품만 (단종·이름 바뀐 옛 제품 제외)."""
    valid = prices[prices.apply_date <= d.isoformat()]
    if valid.empty:
        return []
    cur = valid[valid.apply_date == valid.apply_date.max()]
    cur = cur.dropna(subset=["product_code"]).drop_duplicates(["product_code", "product_name"])
    return [f"{c} - {n}" for c, n in sorted(zip(cur.product_code, cur.product_name))]


def previous_unpaid(partner_name, d: date):
    """부가세 제외 운반비 기준 미결제 (일반 사용자는 자기 전표만)."""
    sc, sp = scope("invoices")
    df = q(f"""SELECT COALESCE(SUM(fee_total), 0) AS t, COALESCE(SUM(paid), 0) AS p
               FROM invoices WHERE partner_name = :n AND invoice_date <= :d AND {sc}""",
           n=partner_name, d=d.isoformat(), **sp)
    return max(0.0, float(df.t[0]) - float(df.p[0]))


def next_serial(conn, d: date):
    prefix = f"S{d:%y%m}"
    row = conn.execute(
        text("SELECT serial_no FROM invoices WHERE serial_no LIKE :p ORDER BY serial_no DESC LIMIT 1"),
        {"p": prefix + "-%"},
    ).fetchone()
    seq = int(row[0].split("-")[1]) + 1 if row else 1
    return f"{prefix}-{seq:04d}"


def round_half_up(x, places=0):
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP))


def calc_fee(qty, price):
    """운반비 = 수량 × 단가(소수 둘째 자리), 원 단위 반올림."""
    from decimal import Decimal
    return round_half_up(Decimal(str(float(qty))) * Decimal(str(float(price))))


def won(x):
    return f"{x:,.0f}"


# ───────────────────────── 사용자 / 로그인 ─────────────────────────
@st.cache_resource
def ensure_auth_schema():
    eng = engine()
    with eng.begin() as conn:
        conn.execute(text("""CREATE TABLE IF NOT EXISTS users (
                                 user_id TEXT PRIMARY KEY, name TEXT, pw_hash TEXT NOT NULL,
                                 is_admin INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created_at TEXT)"""))
        conn.execute(text("""CREATE TABLE IF NOT EXISTS login_tokens (
                                 token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires TEXT NOT NULL)"""))
    cols = {c["name"] for c in sa_inspect(eng).get_columns("invoices")}
    for col in ("created_by", "paid_date"):
        if col not in cols:
            with eng.begin() as conn:
                conn.execute(text(f"ALTER TABLE invoices ADD COLUMN {col} TEXT"))
    return True


def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def check_pw(pw, stored):
    try:
        salt, h = stored.split("$")
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(hash_pw(pw, salt).split("$")[1], h)


def me():
    return st.session_state.get("user") or {}


def is_admin():
    return bool(me().get("is_admin"))


def scope(alias="v"):
    """관리자는 전체, 일반 사용자는 자기가 입력한 전표만."""
    if is_admin():
        return "1=1", {}
    return f"{alias}.created_by = :me_id", {"me_id": me().get("user_id")}


ID_RE = re.compile(r"^[A-Za-z]{6}[0-9]{4}$")   # 영문 6자리 + 숫자 4자리 (예: suyang1234)
PW_RE = re.compile(r"^[0-9]{6}$")              # 숫자 6자리
ID_HELP = "아이디는 영문 6자리 + 숫자 4자리로 만드세요. (예: suyang1234)"
PW_HELP = "비밀번호는 숫자 6자리로 만드세요."


KEEP_DAYS = 90


def issue_token(user_id):
    tok = pysecrets.token_urlsafe(24)
    with engine().begin() as conn:
        conn.execute(text("INSERT INTO login_tokens (token, user_id, expires) VALUES (:t, :u, :e)"),
                     {"t": tok, "u": user_id, "e": (date.today() + timedelta(days=KEEP_DAYS)).isoformat()})
    st.query_params["t"] = tok
    st.session_state.token = tok


def login():
    ensure_auth_schema()
    if me():
        return True
    tok = st.query_params.get("t")
    if tok:
        row = q("""SELECT u.user_id, u.name, u.is_admin FROM login_tokens t
                   JOIN users u ON u.user_id = t.user_id
                   WHERE t.token = :t AND t.expires >= :today AND u.active = 1""",
                t=tok, today=date.today().isoformat())
        if not row.empty:
            st.session_state.user = {"user_id": row.user_id[0], "name": row.name[0],
                                     "is_admin": bool(row.is_admin[0])}
            st.session_state.token = tok
            return True
        del st.query_params["t"]
    st.title("🍺 맥주 운송전표")
    n_users = int(q("SELECT COUNT(*) AS n FROM users").n[0])

    if n_users == 0:
        st.info("처음 사용하는 설정입니다. 관리자 계정을 만드세요. 지금까지 저장된 전표는 모두 이 관리자 것으로 정리됩니다.")
        with st.form("setup"):
            app_pw = st.text_input("기존 앱 접속 비밀번호 (APP_PASSWORD)", type="password")
            uid = st.text_input("관리자 아이디 (영문 6자리 + 숫자 4자리)", max_chars=10)
            name = st.text_input("이름")
            pw1 = st.text_input("비밀번호 (숫자 6자리)", type="password", max_chars=6)
            pw2 = st.text_input("비밀번호 확인", type="password", max_chars=6)
            if st.form_submit_button("관리자 계정 만들기", type="primary"):
                expected = secret("APP_PASSWORD")
                if expected and not hmac.compare_digest(app_pw, str(expected)):
                    st.error("기존 앱 접속 비밀번호가 맞지 않습니다.")
                elif not ID_RE.match(uid.strip()):
                    st.error(ID_HELP)
                elif not PW_RE.match(pw1):
                    st.error(PW_HELP)
                elif pw1 != pw2:
                    st.error("비밀번호 확인이 다릅니다.")
                else:
                    with engine().begin() as conn:
                        conn.execute(text("""INSERT INTO users (user_id, name, pw_hash, is_admin, active, created_at)
                                             VALUES (:u, :n, :h, 1, 1, :c)"""),
                                     {"u": uid.strip(), "n": name.strip() or uid.strip(), "h": hash_pw(pw1),
                                      "c": datetime.now().isoformat(timespec="seconds")})
                        conn.execute(text("UPDATE invoices SET created_by = :u WHERE created_by IS NULL"),
                                     {"u": uid.strip()})
                    st.session_state.user = {"user_id": uid.strip(), "name": name.strip() or uid.strip(),
                                             "is_admin": True}
                    st.rerun()
        return False

    with st.form("login"):
        uid = st.text_input("아이디")
        pw = st.text_input("비밀번호 (숫자 6자리)", type="password", max_chars=6)
        keep = st.checkbox("이 기기에서 로그인 상태 유지 (자동 로그인)", value=True)
        if st.form_submit_button("로그인", type="primary"):
            row = q("SELECT user_id, name, pw_hash, is_admin FROM users WHERE user_id = :u AND active = 1",
                    u=uid.strip())
            if not row.empty and check_pw(pw, row.pw_hash[0]):
                st.session_state.user = {"user_id": row.user_id[0], "name": row.name[0],
                                         "is_admin": bool(row.is_admin[0])}
                if keep:
                    issue_token(row.user_id[0])
                st.rerun()
            st.error("아이디 또는 비밀번호가 맞지 않습니다.")
    return False


def logout():
    tok = st.session_state.get("token")
    if tok:
        with engine().begin() as conn:
            conn.execute(text("DELETE FROM login_tokens WHERE token = :t"), {"t": tok})
    st.query_params.clear()
    st.session_state.clear()
    st.rerun()


APP_URL = "https://suyang-beer.streamlit.app"


def personal_url():
    base = str(secret("APP_URL", APP_URL)).rstrip("/")
    tok = st.session_state.get("token")
    return f"{base}/?embed=true" + (f"&t={tok}" if tok else "")


@st.dialog("📲 바로가기 만들기")
def shortcut_dialog():
    url = personal_url()
    if st.session_state.get("token"):
        st.success("자동 로그인이 들어간 내 전용 바로가기입니다. 다른 사람에게 보내지 마세요.")
    else:
        st.warning("지금은 자동 로그인 없이 로그인했습니다. 바로가기를 열 때마다 로그인해야 합니다. "
                   "자동 로그인을 원하면 로그아웃 후 '로그인 상태 유지'를 체크해서 다시 로그인하세요.")

    st.markdown("**💻 PC (윈도우)**")
    url_file = f"[InternetShortcut]\r\nURL={url}\r\n"
    st.download_button("바탕화면 바로가기 파일 받기", url_file.encode("utf-8"), file_name="맥주 운송전표.url",
                       mime="application/internet-shortcut", use_container_width=True)
    st.caption("받은 '맥주 운송전표.url' 파일을 바탕화면으로 옮기면 더블클릭으로 바로 열립니다.")

    st.markdown("**📱 휴대폰**")
    st.caption("아래 주소 오른쪽의 복사 버튼을 누른 뒤, 휴대폰 브라우저 주소창에 붙여 넣어 여세요. 그다음:")
    st.code(url, language=None)
    st.markdown("- **안드로이드(크롬)**: 오른쪽 위 **⋮** → **홈 화면에 추가**\n"
                "- **아이폰(사파리)**: 아래 **공유(□↑)** → **홈 화면에 추가**")
    st.caption("지금 이 기기에서 만들 때는 주소를 복사할 필요 없이, 브라우저 메뉴에서 바로 '홈 화면에 추가'를 누르면 됩니다.")


def account_menu():
    u = me()
    with st.popover(f"👤 {u['name']}", use_container_width=True):
        st.markdown(f"**{u['name']}** ({u['user_id']}{', 관리자' if u['is_admin'] else ''})")
        if st.session_state.get("token"):
            st.caption("자동 로그인 사용 중입니다. 지금 주소를 휴대폰 홈 화면에 추가하면 바로 열립니다. "
                       "이 주소는 다른 사람에게 보내지 마세요.")
        if st.button("📲 바로가기 만들기", use_container_width=True):
            shortcut_dialog()
        if st.button("로그아웃", use_container_width=True):
            logout()
        if st.button("🔄 기준정보 새로고침", use_container_width=True):
            clear_caches()
            st.rerun()
        st.markdown("**비밀번호 변경**")
        with st.form("chpw", clear_on_submit=True):
            old = st.text_input("현재 비밀번호", type="password", max_chars=6)
            new1 = st.text_input("새 비밀번호 (숫자 6자리)", type="password", max_chars=6)
            new2 = st.text_input("새 비밀번호 확인", type="password", max_chars=6)
            if st.form_submit_button("변경"):
                row = q("SELECT pw_hash FROM users WHERE user_id = :u", u=u["user_id"])
                if row.empty or not check_pw(old, row.pw_hash[0]):
                    st.error("현재 비밀번호가 맞지 않습니다.")
                elif not PW_RE.match(new1):
                    st.error(PW_HELP)
                elif new1 != new2:
                    st.error("새 비밀번호 확인이 다릅니다.")
                else:
                    with engine().begin() as conn:
                        conn.execute(text("UPDATE users SET pw_hash = :h WHERE user_id = :u"),
                                     {"h": hash_pw(new1), "u": u["user_id"]})
                        # 비밀번호를 바꾸면 다른 기기의 자동 로그인은 해제
                        conn.execute(text("DELETE FROM login_tokens WHERE user_id = :u AND token <> :t"),
                                     {"u": u["user_id"], "t": st.session_state.get("token") or ""})
                    st.success("변경했습니다.")


def page_users():
    st.header("사용자 관리")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)
    users = q("""SELECT user_id AS 아이디, name AS 이름, is_admin AS 관리자, active AS 사용,
                        created_at AS 등록일 FROM users ORDER BY created_at""")
    users["관리자"] = users["관리자"].astype(bool)
    users["사용"] = users["사용"].astype(bool)
    counts = q("SELECT created_by, COUNT(*) AS n FROM invoices GROUP BY created_by")
    users["전표 수"] = users["아이디"].map(dict(zip(counts.created_by, counts.n))).fillna(0).astype(int)
    st.dataframe(users, hide_index=True, use_container_width=True)

    st.subheader("새 사용자 만들기")
    with st.form("add_user", clear_on_submit=True):
        c1, c2 = st.columns(2)
        uid = c1.text_input("아이디 (영문 6자리 + 숫자 4자리)", max_chars=10)
        name = c2.text_input("이름")
        c1, c2, c3 = st.columns([2, 2, 1])
        pw1 = c1.text_input("비밀번호 (숫자 6자리)", type="password", max_chars=6)
        pw2 = c2.text_input("비밀번호 확인", type="password", max_chars=6)
        adm = c3.checkbox("관리자")
        if st.form_submit_button("만들기", type="primary"):
            if not ID_RE.match(uid.strip()):
                st.error(ID_HELP)
            elif not PW_RE.match(pw1):
                st.error(PW_HELP)
            elif pw1 != pw2:
                st.error("비밀번호 확인이 다릅니다.")
            else:
                try:
                    with engine().begin() as conn:
                        conn.execute(text("""INSERT INTO users (user_id, name, pw_hash, is_admin, active, created_at)
                                             VALUES (:u, :n, :h, :a, 1, :c)"""),
                                     {"u": uid.strip(), "n": name.strip() or uid.strip(), "h": hash_pw(pw1),
                                      "a": 1 if adm else 0, "c": datetime.now().isoformat(timespec="seconds")})
                    st.session_state.flash = f"{uid.strip()} 사용자를 만들었습니다."
                    st.rerun()
                except IntegrityError:
                    st.error("이미 있는 아이디입니다.")

    st.subheader("사용자 변경")
    target = st.selectbox("사용자", users["아이디"].tolist(), index=None, placeholder="변경할 사용자 선택")
    if target:
        row = users[users["아이디"] == target].iloc[0]
        admins = int(users[users["관리자"] & users["사용"]].shape[0])
        c1, c2 = st.columns(2)
        with c1.form("reset_pw", clear_on_submit=True):
            npw = st.text_input("새 비밀번호 (숫자 6자리)", type="password", max_chars=6)
            if st.form_submit_button("비밀번호 초기화"):
                if not PW_RE.match(npw):
                    st.error(PW_HELP)
                else:
                    with engine().begin() as conn:
                        conn.execute(text("UPDATE users SET pw_hash = :h WHERE user_id = :u"),
                                     {"h": hash_pw(npw), "u": target})
                        conn.execute(text("DELETE FROM login_tokens WHERE user_id = :u"), {"u": target})
                    st.session_state.flash = f"{target} 비밀번호를 바꿨습니다."
                    st.rerun()
        with c2:
            new_admin = st.checkbox("관리자", value=bool(row["관리자"]), key=f"adm_{target}")
            new_active = st.checkbox("사용 (끄면 로그인 불가, 기록은 보존)", value=bool(row["사용"]), key=f"act_{target}")
            if st.button("저장", key=f"save_{target}"):
                losing_admin = bool(row["관리자"]) and bool(row["사용"]) and not (new_admin and new_active)
                if losing_admin and admins <= 1:
                    st.error("관리자가 최소 한 명은 있어야 합니다.")
                elif target == me()["user_id"] and not new_active:
                    st.error("자기 자신은 사용 중지할 수 없습니다.")
                else:
                    with engine().begin() as conn:
                        conn.execute(text("UPDATE users SET is_admin = :a, active = :t WHERE user_id = :u"),
                                     {"a": int(new_admin), "t": int(new_active), "u": target})
                        if not new_active:
                            conn.execute(text("DELETE FROM login_tokens WHERE user_id = :u"), {"u": target})
                    st.session_state.flash = f"{target} 설정을 저장했습니다."
                    st.rerun()


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
    slip_suffix = c2.text_input(f"전표번호  ({inv_date:%Y%m}_ 뒤 6자리까지)", key="slip_suffix", max_chars=6,
                                placeholder="번호 입력 후 Enter")
    if not slip_suffix.strip():
        st.warning("전표번호를 먼저 입력하세요. 전표번호를 입력해야 다음 항목이 나타납니다.")
        return

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
    product_labels = current_product_labels(prices, inv_date) or \
        [f"{r.product_code} - {r.product_name}" for r in products.itertuples()]
    if product_type == "용기":  # 용기는 201로 시작하는 코드를 먼저 보여 준다
        product_labels.sort(key=lambda l: (not l.startswith(CONTAINER_PREFIX), l))
    ver = ss.setdefault("editor_ver", 0)
    cart = ss.setdefault("cart", [])  # [{"label": "코드 - 이름", "qty": 수량}]
    addv = ss.setdefault("add_ver", 0)

    with st.container(border=True):
        kw = st.text_input("제품 검색", key=f"kw_{ver}_{addv}",
                           placeholder="예: 생맥주, 유흥, 캔, 테라 500 (띄어 쓰면 모두 포함된 제품)")
        if kw.strip():
            matches = search_products(product_labels, kw)
        elif product_type == "용기":
            matches = [l for l in product_labels if l.startswith(CONTAINER_PREFIX)]
        else:
            matches = []
        pick = None
        if kw.strip() and not matches:
            st.caption("검색 결과가 없습니다. 다른 단어로 찾아보세요.")
        if matches:
            if len(matches) > MAX_MATCHES:
                st.caption(f"{len(matches)}개 중 {MAX_MATCHES}개만 보여 줍니다. 검색어를 더 입력하세요.")
            pick = st.radio("제품 선택", matches[:MAX_MATCHES], index=None, key=f"pick_{ver}_{addv}")
        c1, c2 = st.columns([2, 1])
        qty_new = c1.number_input("수량", min_value=0, step=1, value=None, key=f"qty_{ver}_{addv}")
        c2.write("")
        if c2.button("➕ 추가", use_container_width=True, disabled=not (pick and qty_new),
                     key=f"add_{ver}_{addv}"):
            cart.append({"label": pick, "qty": float(qty_new)})
            ss.add_ver = addv + 1
            st.rerun()

    for i, it in enumerate(list(cart)):
        c1, c2, c3 = st.columns([5, 2, 1])
        c1.write(it["label"])
        it["qty"] = float(c2.number_input("수량", min_value=0, step=1, value=int(it["qty"]),
                                          key=f"cq_{ver}_{i}_{it['label']}", label_visibility="collapsed"))
        if c3.button("❌", key=f"rm_{ver}_{i}_{it['label']}"):
            cart.pop(i)
            st.rerun()

    items, missing = [], []
    for it in cart:
        if not it["qty"]:
            continue
        code, name = it["label"].split(" - ", 1)
        qty = it["qty"]
        price = lookup_price(prices, name, price_dest, inv_date) if price_dest else None
        if price is None:
            missing.append(name)
            price = 0.0
        items.append({"product_code": code, "product_name": name, "qty": float(qty),
                      "unit_price": price, "fee": calc_fee(qty, price)})

    if items:
        view = pd.DataFrame(items).rename(columns={
            "product_code": "제품코드", "product_name": "제품명", "qty": "수량",
            "unit_price": "단가", "fee": "운반비"})
        st.dataframe(view.style.format({"수량": "{:,.0f}", "단가": "{:,.2f}", "운반비": "{:,.0f}"}),
                     use_container_width=True, hide_index=True)
    if missing:
        st.warning(f"단가가 없는 제품: {', '.join(missing)} (도착지 {price_dest}, {inv_date} 기준) — 0원으로 계산됩니다.")

    # 금액 / 미수금
    fee_total = sum(i["fee"] for i in items)
    vat = round(fee_total * VAT_RATE)
    total = fee_total + vat
    st.subheader("금액")
    st.metric("운반비", won(fee_total))
    st.caption("결제는 '결제 관리' 메뉴에서 익월 20일 기준으로 한꺼번에 처리합니다.")

    if st.button("💾 저장", type="primary", use_container_width=True):
        if not origin or not dest:
            st.error("출발지와 도착지를 선택하세요.")
            return
        if origin == dest:
            st.error("출발지와 도착지가 같습니다. 다시 확인하세요.")
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
                            fee_total, vat, total, paid, created_at, created_by)
                        VALUES (:serial_no, :invoice_date, :slip_no, :partner_name, :biz_no,
                            :origin, :dest, :product_type, :unload_type, :empty_type,
                            :fee_total, :vat, :total, :paid, :created_at, :created_by)"""), {
                        "serial_no": serial, "invoice_date": inv_date.isoformat(),
                        "slip_no": f"{inv_date:%Y%m}_{slip_suffix.strip()}",
                        "partner_name": partner["name"], "biz_no": partner["biz_no"],
                        "origin": origin, "dest": dest, "product_type": product_type,
                        "unload_type": unload_type, "empty_type": empty_type,
                        "fee_total": fee_total, "vat": vat, "total": total, "paid": 0.0,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "created_by": me().get("user_id"),
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

        st.session_state.flash = f"{serial} 저장 완료 (운반비 {won(fee_total)}원)"
        st.session_state.editor_ver += 1
        st.session_state.cart = []
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
    options = current_product_labels(load_prices(), new_date)
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
                     "unit_price": float(price), "fee": calc_fee(qty, price)})
    if rows:
        st.dataframe(pd.DataFrame(rows).rename(columns={
            "product_code": "제품코드", "product_name": "제품명", "qty": "수량",
            "unit_price": "단가", "fee": "운반비"}).style.format(
            {"수량": "{:,.0f}", "단가": "{:,.2f}", "운반비": "{:,.0f}"}),
            hide_index=True, use_container_width=True)
    if missing:
        st.warning(f"단가표에 없는 제품: {', '.join(missing)} ({price_dest}, {new_date} 기준) — 0원으로 계산됩니다.")

    fee_total = sum(x["fee"] for x in rows)
    vat = round(fee_total * VAT_RATE)
    total = fee_total + vat

    st.metric("운반비", won(fee_total), delta=won(fee_total - inv.fee_total) if fee_total != inv.fee_total else None)
    is_paid = isinstance(inv.get("paid_date"), str) and bool(inv.get("paid_date"))
    if is_paid:
        st.caption(f"이 전표는 {inv.paid_date}에 결제 처리되었습니다. 수정하면 결제 금액도 새 운반비로 맞춰집니다.")
    new_paid = fee_total if is_paid else float(inv.paid or 0)

    b1, b2 = st.columns([3, 1])
    if b1.button("✏️ 수정 내용 저장", type="primary", use_container_width=True, key=k + "save"):
        if not rows:
            st.error("제품 줄이 하나 이상 있어야 합니다. 전표를 없애려면 삭제를 이용하세요.")
            return
        if new_origin == new_dest:
            st.error("출발지와 도착지가 같습니다. 다시 확인하세요.")
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
        st.session_state.sel_serials = []
        st.session_state.list_ver = st.session_state.get("list_ver", 0) + 1
        st.rerun()

    with b2.popover("🗑️ 삭제", use_container_width=True):
        st.write(f"{serial} 전표를 완전히 삭제할까요?")
        if st.button("삭제 확인", type="primary", key=k + "del"):
            with engine().begin() as conn:
                conn.execute(text("DELETE FROM invoice_items WHERE serial_no = :s"), {"s": serial})
                conn.execute(text("DELETE FROM invoices WHERE serial_no = :s"), {"s": serial})
            st.session_state.flash = f"{serial} 삭제 완료"
            st.session_state.sel_serials = []
            st.session_state.list_ver = st.session_state.get("list_ver", 0) + 1
            st.rerun()


def delete_item_rows(ids, serials):
    """선택한 제품 줄을 지우고 전표 합계를 다시 계산. 줄이 하나도 안 남은 전표는 전표째 삭제."""
    with engine().begin() as conn:
        conn.execute(text("DELETE FROM invoice_items WHERE id = :i"), [{"i": int(i)} for i in ids])
        for sn in serials:
            fee = conn.execute(text("SELECT COUNT(*), COALESCE(SUM(fee), 0) FROM invoice_items WHERE serial_no = :s"),
                               {"s": sn}).fetchone()
            if fee[0] == 0:
                conn.execute(text("DELETE FROM invoices WHERE serial_no = :s"), {"s": sn})
            else:
                f = float(fee[1])
                vat = round(f * VAT_RATE)
                conn.execute(text("UPDATE invoices SET fee_total = :f, vat = :v, total = :t, paid = CASE WHEN paid_date IS NOT NULL THEN :f ELSE paid END WHERE serial_no = :s"),
                             {"f": f, "v": float(vat), "t": f + vat, "s": sn})


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

    sc, sp = scope("v")
    where = f"v.invoice_date BETWEEN :a AND :b AND {sc}"
    params = {"a": d_from.isoformat(), "b": d_to.isoformat(), **sp}
    if is_admin():
        writers = q("SELECT user_id, name FROM users ORDER BY user_id")
        wlabels = ["전체"] + [f"{r.user_id} ({r.name})" for r in writers.itertuples()]
        wsel = st.selectbox("작성자 (관리자 전용)", wlabels)
        if wsel != "전체":
            where += " AND v.created_by = :w"
            params["w"] = wsel.split(" (")[0]
    if who != "전체":
        where += " AND v.partner_name = :n"
        params["n"] = who
    df = q(f"SELECT v.* FROM invoices v WHERE {where} ORDER BY v.invoice_date, v.serial_no", **params)
    if df.empty:
        st.info("이 기간에 저장된 전표가 없습니다.")
        return

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("전표 수", f"{len(df)}건")
    s2.metric("운반비", won(df.fee_total.sum()))
    s3.metric("결제완료", won(df.paid.sum()))
    s4.metric("미결제", won(df.fee_total.sum() - df.paid.sum()))

    items = q(f"""SELECT i.id, v.serial_no, v.invoice_date, v.slip_no, v.origin, v.dest, v.created_by, v.paid_date,
                         i.product_code,
                         i.product_name, i.qty, i.unit_price, i.fee
                  FROM invoice_items i JOIN invoices v ON v.serial_no = i.serial_no
                  WHERE {where} ORDER BY v.invoice_date, v.serial_no, i.id""", **params).reset_index(drop=True)

    # 선택은 전표 단위: 한 줄만 체크해도 같은 전표의 모든 줄이 함께 체크된다
    ss = st.session_state
    chosen = [sn for sn in ss.get("sel_serials", []) if sn in set(items["serial_no"])]
    view = pd.DataFrame({
        "선택": items["serial_no"].isin(chosen),
        "날짜": items["invoice_date"], "전표번호": items["slip_no"].fillna(""),
        "출발지": items["origin"], "도착지": items["dest"], "제품명": items["product_name"],
        "수량": items["qty"].map(lambda v: f"{v:,.0f}"),
        "단가": items["unit_price"].map(lambda v: f"{v:,.2f}"),
        "운반비": items["fee"].map(lambda v: f"{v:,.0f}"),
        # 전표별 합계운반비: 전표의 첫 줄에만 표시
        "전표 합계": pd.Series([f"{t:,.0f}" if first else "" for t, first in zip(
            items.groupby("serial_no")["fee"].transform("sum"), ~items["serial_no"].duplicated())],
            index=items.index),
        "결제": items["paid_date"].fillna("").map(lambda d: f"✅ {d}" if d else "미결제"),
        "일련번호": items["serial_no"],
    })
    if is_admin():
        view["작성자"] = items["created_by"]
    st.caption("맨 왼쪽 칸을 체크하면 같은 전표의 모든 품목이 함께 선택되고, 아래에 수정·삭제가 나타납니다.")
    ver = ss.setdefault("list_ver", 0)
    edited = st.data_editor(
        view, hide_index=True, use_container_width=True, key=f"item_table_{ver}",
        disabled=[c for c in view.columns if c != "선택"],
        column_config={"선택": st.column_config.CheckboxColumn("선택", width="small")},
    )
    flipped = edited.index[edited["선택"] != view["선택"]]
    if len(flipped):
        new_sel = list(chosen)
        for i in flipped:
            sn = items.at[i, "serial_no"]
            if bool(edited.at[i, "선택"]) and sn not in new_sel:
                new_sel.append(sn)
            elif not bool(edited.at[i, "선택"]) and sn in new_sel:
                new_sel.remove(sn)
        ss.sel_serials = new_sel
        ss.list_ver = ver + 1
        st.rerun()

    if chosen:
        sel = items[items["serial_no"].isin(chosen)]
        st.divider()
        st.write(f"선택한 전표 **{len(chosen)}건** (품목 {len(sel)}줄): "
                 + ", ".join(f"{sn} ({df.loc[df.serial_no == sn, 'slip_no'].iloc[0] or '-'})" for sn in chosen))
        b1, b2 = st.columns([1, 4])
        if b1.button("선택 해제"):
            ss.sel_serials = []
            ss.list_ver = ver + 1
            st.rerun()
        with b2.popover(f"🗑️ 선택한 전표 {len(chosen)}건 삭제"):
            st.write("선택한 전표와 그 전표의 모든 품목을 삭제합니다.")
            if st.button("삭제 확인", type="primary", key="del_invoices"):
                with engine().begin() as conn:
                    for sn in chosen:
                        conn.execute(text("DELETE FROM invoice_items WHERE serial_no = :s"), {"s": sn})
                        conn.execute(text("DELETE FROM invoices WHERE serial_no = :s"), {"s": sn})
                ss.sel_serials = []
                ss.list_ver = ver + 1
                ss.flash = f"전표 {len(chosen)}건을 삭제했습니다."
                st.rerun()

        tabs = st.tabs([f"✏️ {sn} 수정" for sn in chosen])
        for tab, sn in zip(tabs, chosen):
            with tab:
                edit_invoice(sn, df[df.serial_no == sn].iloc[0])

    # 엑셀 다운로드
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.rename(columns={"serial_no": "일련번호", "invoice_date": "작성일자", "slip_no": "전표번호",
                           "partner_name": "상호", "biz_no": "사업자번호", "origin": "출발지", "dest": "도착지",
                           "product_type": "제품구분", "unload_type": "하차구분", "empty_type": "공차구분",
                           "fee_total": "운반비", "paid": "결제완료", "created_by": "작성자"}
                  ).drop(columns=["created_at", "vat", "total"], errors="ignore").to_excel(xw, sheet_name="전표", index=False)
        items.drop(columns=["id"]).rename(columns={
            "serial_no": "일련번호", "invoice_date": "날짜", "origin": "출발지", "dest": "도착지",
            "product_code": "제품코드", "product_name": "제품명", "qty": "수량", "unit_price": "단가",
            "fee": "운반비", "created_by": "작성자"}).to_excel(xw, sheet_name="제품내역", index=False)
    st.download_button("📥 엑셀로 내려받기", buf.getvalue(), file_name=f"운송전표_{d_from}_{d_to}.xlsx",
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


# ───────────────────────── 결제 관리 (익월 20일 일괄결제) ─────────────────────────
WEEKDAYS = "월화수목금토일"


def due_date_for(ym):
    y, m = map(int, ym.split("-"))
    y2, m2 = (y, m + 1) if m < 12 else (y + 1, 1)
    return date(y2, m2, 20)


def page_payments():
    st.header("결제 관리")
    st.caption("전표 작성월의 운반비는 다음 달 20일에 한꺼번에 결제합니다. "
               "20일이 휴일이면 결제 처리일을 실제 결제한 날로 바꿔서 처리하세요.")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)

    sc, sp = scope("v")
    months = q(f"SELECT DISTINCT substr(v.invoice_date, 1, 7) AS ym FROM invoices v WHERE {sc} ORDER BY ym DESC",
               **sp)["ym"].tolist()
    if not months:
        st.info("저장된 전표가 없습니다.")
        return
    last_month = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    c1, c2, c3 = st.columns(3)
    ym = c1.selectbox("대상 월 (전표 작성월)", months,
                      index=months.index(last_month) if last_month in months else 0)
    due = due_date_for(ym)
    c2.metric("정기 결제일", f"{due} ({WEEKDAYS[due.weekday()]})")
    pay_date = c3.date_input("결제 처리일", value=due, format="YYYY-MM-DD", key=f"paydate_{ym}")
    if due.weekday() >= 5:
        st.warning(f"{due}은(는) {WEEKDAYS[due.weekday()]}요일입니다. 실제 결제한 날로 결제 처리일을 바꾸세요.")
    else:
        st.caption("공휴일은 자동으로 알 수 없으니, 20일이 공휴일이면 결제 처리일을 직접 바꾸세요.")

    inv = q(f"""SELECT v.serial_no, v.invoice_date, v.slip_no, v.partner_name, v.fee_total, v.paid,
                       v.paid_date, v.created_by
                FROM invoices v WHERE substr(v.invoice_date, 1, 7) = :ym AND {sc}
                ORDER BY v.partner_name, v.invoice_date""", ym=ym, **sp)
    inv["done"] = inv["paid_date"].notna() | ((inv["fee_total"] > 0) & (inv["paid"] >= inv["fee_total"]))

    m1, m2, m3 = st.columns(3)
    m1.metric(f"{ym} 운반비", won(inv.fee_total.sum()))
    m2.metric("결제완료", won(inv.loc[inv.done, "fee_total"].sum()))
    m3.metric("미결제", won(inv.loc[~inv.done, "fee_total"].sum()))

    g = inv.groupby("partner_name").agg(전표수=("serial_no", "count"), 운반비=("fee_total", "sum"),
                                        결제완료=("done", "sum"),
                                        결제일=("paid_date", lambda x: ", ".join(sorted(set(x.dropna())))))
    g["상태"] = ["✅ 결제완료" if r.결제완료 == r.전표수 else ("미결제" if r.결제완료 == 0 else "일부 결제")
                for r in g.itertuples()]
    g = g.reset_index().rename(columns={"partner_name": "거래처"})
    table = pd.DataFrame({"선택": False, "거래처": g["거래처"], "전표 수": g["전표수"],
                          "운반비": g["운반비"].map(won), "상태": g["상태"], "결제일": g["결제일"]})
    edited = st.data_editor(table, hide_index=True, use_container_width=True, key=f"pay_tbl_{ym}",
                            disabled=[c for c in table.columns if c != "선택"],
                            column_config={"선택": st.column_config.CheckboxColumn("선택", width="small")})
    chosen = edited.loc[edited["선택"], "거래처"].tolist()

    b1, b2 = st.columns(2)
    if b1.button(f"✅ 선택 거래처 일괄 결제 처리 ({pay_date})", type="primary",
                 disabled=not chosen, use_container_width=True):
        usc, usp = scope("invoices")
        with engine().begin() as conn:
            for pn in chosen:
                conn.execute(text(f"""UPDATE invoices SET paid = fee_total, paid_date = :d
                                      WHERE substr(invoice_date, 1, 7) = :ym AND partner_name = :pn
                                        AND paid_date IS NULL AND {usc}"""),
                             {"d": pay_date.isoformat(), "ym": ym, "pn": pn, **usp})
        st.session_state.flash = f"{ym} 전표 결제 처리 완료 ({', '.join(chosen)}, 결제일 {pay_date})"
        st.rerun()
    with b2.popover("↩️ 선택 거래처 결제 취소", use_container_width=True, disabled=not chosen):
        st.write(f"{ym} {', '.join(chosen)} 전표의 결제 처리를 취소합니다.")
        if st.button("결제 취소 확인", key="pay_cancel"):
            usc, usp = scope("invoices")
            with engine().begin() as conn:
                for pn in chosen:
                    conn.execute(text(f"""UPDATE invoices SET paid = 0, paid_date = NULL
                                          WHERE substr(invoice_date, 1, 7) = :ym AND partner_name = :pn AND {usc}"""),
                                 {"ym": ym, "pn": pn, **usp})
            st.session_state.flash = f"{ym} {', '.join(chosen)} 결제 처리를 취소했습니다."
            st.rerun()

    with st.expander(f"{ym} 전표 목록 보기"):
        detail = pd.DataFrame({"날짜": inv["invoice_date"], "전표번호": inv["slip_no"], "거래처": inv["partner_name"],
                               "운반비": inv["fee_total"].map(won),
                               "결제": inv["paid_date"].fillna("").map(lambda d: f"✅ {d}" if d else "미결제")})
        if is_admin():
            detail["작성자"] = inv["created_by"]
        st.dataframe(detail, hide_index=True, use_container_width=True)


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


def round_price(x):
    """새 단가는 소수 둘째 자리까지 (셋째 자리에서 반올림)."""
    return round_half_up(x, 2)


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

    price_change_judgement(fuel_map)
    st.divider()
    reapply_section()


def price_change_judgement(fuel_map):
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
    st.caption("새 단가는 소수 둘째 자리까지 계산합니다 (셋째 자리에서 반올림).")
    # 직전 적용일자의 단가표(현재 단가)만 대상 — 옛날에 끝난 제품·이름이 바뀐 제품은 제외
    before = prices[prices["apply_date"] < apply_date.isoformat()]
    latest = before[before["apply_date"] == before["apply_date"].max()].drop_duplicates(["product_name", "dest"])
    latest["새단가"] = [round_price(p * (1 + rate)) for p in latest["price"]]
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


def reapply_section():
    st.subheader("④ 이미 입력한 전표에 단가 다시 적용")
    st.caption("새 단가를 저장하기 전에 입력한 전표가 있으면, 작성일자 기준 단가표 단가로 다시 계산합니다. "
               "결제완료 금액은 바꾸지 않습니다.")
    prices = load_prices()
    if prices.empty:
        return
    from_d = st.date_input("작성일자가 이 날짜 이후(포함)인 전표",
                           value=date.fromisoformat(prices["apply_date"].max()),
                           format="YYYY-MM-DD", key="reapply_from")
    invs = q("""SELECT serial_no, invoice_date, origin, dest, product_type, fee_total
                FROM invoices WHERE invoice_date >= :d""", d=from_d.isoformat())
    if invs.empty:
        st.info("해당 기간에 입력된 전표가 없습니다.")
        return
    items = q("""SELECT i.id, i.serial_no, i.product_name, i.qty, i.unit_price
                 FROM invoice_items i JOIN invoices v ON v.serial_no = i.serial_no
                 WHERE v.invoice_date >= :d""", d=from_d.isoformat())
    inv_map = invs.set_index("serial_no")

    changes = []
    for it in items.itertuples(index=False):
        inv = inv_map.loc[it.serial_no]
        pdest = inv.origin if inv.product_type in SWAP_TYPES else inv.dest
        new_p = lookup_price(prices, it.product_name, pdest, date.fromisoformat(inv.invoice_date))
        if new_p is not None and abs(new_p - float(it.unit_price or 0)) > 0.001:
            changes.append({"id": it.id, "serial_no": it.serial_no, "qty": float(it.qty or 0),
                            "old": float(it.unit_price or 0), "new": new_p, "fee": calc_fee(it.qty or 0, new_p)})
    if not changes:
        st.success("모든 전표가 작성일자 기준 단가로 계산되어 있습니다. 다시 적용할 전표가 없습니다.")
        return

    ch = pd.DataFrame(changes)
    new_fee = (items.assign(fee_new=[calc_fee(r.qty or 0, r.unit_price or 0) for r in items.itertuples()])
               .set_index("id"))
    for c in changes:
        new_fee.loc[c["id"], "fee_new"] = c["fee"]
    per_inv = new_fee.groupby("serial_no")["fee_new"].sum()
    summary = inv_map.loc[ch["serial_no"].unique(), ["invoice_date", "fee_total"]].copy()
    summary["새 운반비"] = per_inv.reindex(summary.index)
    summary["차이"] = summary["새 운반비"] - summary["fee_total"]
    summary = summary.reset_index().rename(columns={"serial_no": "일련번호", "invoice_date": "작성일자",
                                                    "fee_total": "기존 운반비"})
    st.write(f"단가가 다른 제품 줄 **{len(ch)}건**, 전표 **{len(summary)}건**")
    st.dataframe(summary.style.format({"기존 운반비": "{:,.0f}", "새 운반비": "{:,.0f}", "차이": "{:+,.0f}"}),
                 hide_index=True, use_container_width=True)
    if st.button(f"🔁 전표 {len(summary)}건 단가 다시 적용", type="primary"):
        with engine().begin() as conn:
            conn.execute(text("UPDATE invoice_items SET unit_price = :p, fee = :f WHERE id = :i"),
                         [{"p": c["new"], "f": c["fee"], "i": int(c["id"])} for c in changes])
            for sn, fee_total in per_inv.reindex(summary["일련번호"]).items():
                vat = round(fee_total * VAT_RATE)
                conn.execute(text("UPDATE invoices SET fee_total = :f, vat = :v, total = :t, paid = CASE WHEN paid_date IS NOT NULL THEN :f ELSE paid END WHERE serial_no = :s"),
                             {"f": float(fee_total), "v": float(vat), "t": float(fee_total + vat), "s": sn})
        st.session_state.flash = f"전표 {len(summary)}건의 단가를 다시 적용했습니다."
        st.rerun()


# ───────────────────────── 단가표 조회 ─────────────────────────
def page_price_table():
    st.header("단가표 조회")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)
    prices = q("""SELECT apply_date AS 적용일자, product_code AS 제품코드, product_name AS 제품명,
                         dest AS 도착지, price AS 단가 FROM unit_prices""")
    if prices.empty:
        st.info("단가표가 비어 있습니다.")
        return
    dates = sorted(prices["적용일자"].unique(), reverse=True)

    c1, c2, c3 = st.columns([1, 2, 2])
    mode = c1.radio("보기", ["적용일자별 비교", "전체 목록"])
    prods = c2.multiselect("제품 (비우면 전체)", sorted(prices["제품명"].dropna().unique()))
    dsts = c3.multiselect("도착지 (비우면 전체)", sorted(prices["도착지"].dropna().unique()))
    f = prices
    if prods:
        f = f[f["제품명"].isin(prods)]
    if dsts:
        f = f[f["도착지"].isin(dsts)]

    if mode == "적용일자별 비교":
        sel_dates = st.multiselect("비교할 적용일자", dates, default=dates[:3])
        f = f[f["적용일자"].isin(sel_dates)]
        view = (f.pivot_table(index=["제품코드", "제품명", "도착지"], columns="적용일자", values="단가", aggfunc="last")
                .reindex(columns=sorted(sel_dates, reverse=True)).reset_index())
        st.caption("가로로 적용일자별 단가를 비교합니다. 빈칸은 그 적용일자에 단가가 없다는 뜻입니다.")
    else:
        view = f.sort_values(["제품코드", "도착지", "적용일자"], ascending=[True, True, False])
    num_cols = [c for c in view.columns if c not in ("제품코드", "제품명", "도착지", "적용일자")]
    st.write(f"{len(view):,}줄")
    shown = view.copy()
    for c in num_cols:  # 빈칸이 None으로 보이지 않도록 글자로 표시
        shown[c] = shown[c].map(lambda v: "" if pd.isna(v) else f"{v:,.2f}")
    st.dataframe(shown, hide_index=True, use_container_width=True, height=520)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        view.to_excel(xw, sheet_name="단가표", index=False)
    st.download_button("📥 엑셀로 내려받기", buf.getvalue(), file_name="단가표.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if not is_admin():
        return
    st.divider()
    with st.expander("🗑️ 적용일자별 단가 삭제 (관리자)"):
        st.caption("잘못 들어간 적용일자의 단가를 한꺼번에 지웁니다. 이미 입력한 전표의 단가는 바뀌지 않습니다.")
        d = st.selectbox("삭제할 적용일자", dates, index=None, placeholder="적용일자 선택")
        if d:
            n = int((prices["적용일자"] == d).sum())
            st.warning(f"{d} 적용 단가 {n:,}건을 삭제합니다.")
            if st.checkbox("확인했습니다", key="del_price_ok") and st.button(f"{d} 단가 {n:,}건 삭제", type="primary"):
                with engine().begin() as conn:
                    conn.execute(text("DELETE FROM unit_prices WHERE apply_date = :d"), {"d": d})
                clear_caches()
                st.session_state.flash = f"{d} 적용 단가 {n:,}건을 삭제했습니다."
                st.rerun()


# ───────────────────────── main ─────────────────────────
if login():
    pages = {"전표 입력": page_entry, "전표 조회": page_list, "결제 관리": page_payments,
             "단가표 조회": page_price_table}
    if is_admin():
        pages.update({"단가 변경": page_price_change, "단가·거래처 관리": page_admin, "사용자 관리": page_users})
    names = list(pages)
    c1, c2 = st.columns([5, 1])
    with c1:
        sel = st.segmented_control("메뉴", names, default=st.session_state.get("last_page", names[0]),
                                   key="nav", label_visibility="collapsed")
    with c2:
        account_menu()
    page = sel if sel in pages else st.session_state.get("last_page", names[0])
    if page not in pages:
        page = names[0]
    st.session_state.last_page = page
    pages[page]()
