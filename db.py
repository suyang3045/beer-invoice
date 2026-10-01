"""DB 연결과 테이블 정의 (Supabase PostgreSQL / 로컬 SQLite 공용)."""
from sqlalchemy import create_engine, text

DEFAULT_PARTNER = ("수양물류(주)강원영업소", "326-85-01795")


def normalize_url(url: str) -> str:
    """Supabase가 주는 postgresql://... 주소를 SQLAlchemy용으로 바꾼다."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def make_engine(url: str):
    engine = create_engine(normalize_url(url), pool_pre_ping=True, pool_recycle=300)
    init_schema(engine)
    return engine


def init_schema(engine):
    is_pg = engine.dialect.name == "postgresql"
    serial_pk = "SERIAL PRIMARY KEY" if is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    money = "DOUBLE PRECISION"  # PostgreSQL의 REAL은 7자리 정밀도라 금액에 부적합

    stmts = [
        """CREATE TABLE IF NOT EXISTS partners (
               biz_no TEXT PRIMARY KEY,
               name   TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS destinations (
               code   TEXT PRIMARY KEY,
               origin TEXT NOT NULL,
               dest   TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS unit_prices (
               id           {serial_pk},
               apply_date   TEXT NOT NULL,
               product_code TEXT,
               product_name TEXT NOT NULL,
               dest         TEXT NOT NULL,
               price        {money} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS invoices (
               serial_no    TEXT PRIMARY KEY,
               invoice_date TEXT NOT NULL,
               slip_no      TEXT,
               partner_name TEXT,
               biz_no       TEXT,
               origin       TEXT,
               dest         TEXT,
               product_type TEXT,
               unload_type  TEXT,
               empty_type   TEXT,
               fee_total    {money} DEFAULT 0,
               vat          {money} DEFAULT 0,
               total        {money} DEFAULT 0,
               paid         {money} DEFAULT 0,
               created_at   TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS invoice_items (
               id           {serial_pk},
               serial_no    TEXT NOT NULL REFERENCES invoices(serial_no) ON DELETE CASCADE,
               product_code TEXT,
               product_name TEXT,
               qty          {money},
               unit_price   {money},
               fee          {money})""",
        "CREATE INDEX IF NOT EXISTS idx_up_lookup ON unit_prices(product_name, dest, apply_date)",
        "CREATE INDEX IF NOT EXISTS idx_inv_partner ON invoices(partner_name, invoice_date)",
        "CREATE INDEX IF NOT EXISTS idx_items_serial ON invoice_items(serial_no)",
    ]
    with engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))
        conn.execute(
            text("INSERT INTO partners (biz_no, name) VALUES (:b, :n) ON CONFLICT (biz_no) DO NOTHING"),
            {"n": DEFAULT_PARTNER[0], "b": DEFAULT_PARTNER[1]},
        )
