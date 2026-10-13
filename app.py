"""
발표 동료평가 웹앱 (Streamlit)

- 학생: 학번·이름·비밀번호(숫자 4자리)로 로그인
  · 평가하기: 선생님이 '평가중'으로 연 발표자에게 기준별 점수 + 한 줄 피드백
  · 내가 한 평가: 내가 남긴 평가만 볼 수 있음
  · 내가 받은 피드백: 선생님이 결과를 공개하면 익명으로 모아 보기 + 성찰 쓰기
- 선생님: 비밀번호로 로그인
  · 회차 만들기, 발표자 명단 등록, 발표자별 평가 열기/마감, 결과 공개
  · 결과 표 보기, 엑셀용 CSV 내려받기, 전체 백업/복원
  · 채점기준 수정, 학생 비밀번호 초기화

선생님 비밀번호는 Streamlit Cloud의 Secrets에 TEACHER_PASSWORD = "원하는비밀번호"
로 넣어 두세요. 넣지 않으면 기본값(teacher1234)이 쓰입니다.
"""

import hashlib
import html
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ─────────────────────────────────────────────
# 기본 설정
# ─────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "peer_review.db"
KST = ZoneInfo("Asia/Seoul")
DEFAULT_TEACHER_PASSWORD = "teacher1234"

LEVELS = {3: "잘함", 2: "보통", 1: "노력 필요"}
STATUSES = ["대기", "평가중", "마감"]

DEFAULT_RUBRIC = [
    {"name": "자료 재구성", "desc": "청중이 이해하기 쉽게 자료를 골라 바꾸어 제시했다."},
    {"name": "내용 조직", "desc": "처음-중간-끝이 분명하고 내용의 흐름이 체계적이다."},
    {"name": "전달", "desc": "목소리, 시선, 화면 자료를 활용해 알아듣기 쉽게 전달했다."},
]

MIN_COMMENT_LEN = 5
MAX_COMMENT_LEN = 100


# ─────────────────────────────────────────────
# 데이터베이스
# ─────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M")


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS students (
                sid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                pw_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS presenters (
                round TEXT NOT NULL,
                sid TEXT NOT NULL,
                name TEXT NOT NULL,
                ord INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT '대기',
                PRIMARY KEY (round, sid)
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                round TEXT NOT NULL,
                presenter_sid TEXT NOT NULL,
                presenter_name TEXT NOT NULL,
                evaluator_sid TEXT NOT NULL,
                evaluator_name TEXT NOT NULL,
                scores TEXT NOT NULL,
                comment TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (round, presenter_sid, evaluator_sid)
            );
            CREATE TABLE IF NOT EXISTS reflections (
                round TEXT NOT NULL,
                sid TEXT NOT NULL,
                text TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (round, sid)
            );
            """
        )
    if get_setting("rubric") is None:
        set_setting("rubric", DEFAULT_RUBRIC)
    if get_setting("rounds") is None:
        set_setting("rounds", ["발표 1차"])
        set_setting("current_round", "발표 1차")
    if get_setting("open_rounds") is None:
        set_setting("open_rounds", [])


def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


# ── 학생 계정 ─────────────────────────────────
def hash_pw(sid, pw):
    return hashlib.sha256(f"{sid}:{pw}:peer-review".encode()).hexdigest()


def login_student(sid, name, pw):
    """처음 로그인하면 계정을 만들고, 이후에는 이름·비밀번호를 확인한다."""
    sid, name, pw = sid.strip(), name.strip(), pw.strip()
    if not re.fullmatch(r"\d{3,6}", sid):
        return False, "학번은 숫자로만 입력해 주세요. (예: 20501)"
    if not name:
        return False, "이름을 입력해 주세요."
    if not re.fullmatch(r"\d{4}", pw):
        return False, "비밀번호는 숫자 4자리로 입력해 주세요."
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM students WHERE sid=?", (sid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO students(sid, name, pw_hash, created_at) VALUES(?,?,?,?)",
                (sid, name, hash_pw(sid, pw), now_str()),
            )
            return True, "처음 오셨네요! 방금 입력한 비밀번호가 등록되었어요. 꼭 기억해 두세요."
        if row["name"] != name:
            return False, "학번과 이름이 맞지 않아요. 다시 확인해 주세요."
        if row["pw_hash"] != hash_pw(sid, pw):
            return False, "비밀번호가 틀렸어요. 잊어버렸다면 선생님께 초기화를 부탁하세요."
    return True, ""


def list_students():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT sid, name FROM students ORDER BY sid")]


def reset_student(sid):
    """학생 계정을 지운다. 다음 로그인 때 새 비밀번호로 다시 등록된다. (평가 기록은 그대로)"""
    with get_conn() as conn:
        conn.execute("DELETE FROM students WHERE sid=?", (sid,))


# ── 회차·발표자 ───────────────────────────────
def parse_presenter_lines(text):
    """'20501 김민지' 형식의 줄들을 읽는다. 엑셀에서 복사한 탭/쉼표도 허용."""
    ok, bad = [], []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^\s*(\d{3,6})[\s,\t]+(.+?)\s*$", line)
        if m and m.group(1) not in seen:
            ok.append((m.group(1), m.group(2).strip()))
            seen.add(m.group(1))
        else:
            bad.append(line.strip())
    return ok, bad


def set_presenters(rnd, pairs):
    with get_conn() as conn:
        old = {
            r["sid"]: r["status"]
            for r in conn.execute("SELECT sid, status FROM presenters WHERE round=?", (rnd,))
        }
        conn.execute("DELETE FROM presenters WHERE round=?", (rnd,))
        for i, (sid, name) in enumerate(pairs):
            conn.execute(
                "INSERT INTO presenters(round, sid, name, ord, status) VALUES(?,?,?,?,?)",
                (rnd, sid, name, i, old.get(sid, "대기")),
            )


def list_presenters(rnd):
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM presenters WHERE round=? ORDER BY ord", (rnd,)
            )
        ]


def set_presenter_status(rnd, sid, status):
    with get_conn() as conn:
        conn.execute(
            "UPDATE presenters SET status=? WHERE round=? AND sid=?", (status, rnd, sid)
        )


def get_presenter(rnd, sid):
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM presenters WHERE round=? AND sid=?", (rnd, sid)
        ).fetchone()
    return dict(r) if r else None


# ── 평가 ──────────────────────────────────────
def save_evaluation(rnd, presenter, evaluator, scores, comment):
    """presenter, evaluator: {'sid','name'} 딕셔너리. 같은 사람이 다시 내면 수정된다."""
    p = get_presenter(rnd, presenter["sid"])
    if p is None or p["status"] != "평가중":
        return False, "이 발표자의 평가는 지금 열려 있지 않아요."
    if presenter["sid"] == evaluator["sid"]:
        return False, "자기 자신은 평가할 수 없어요."
    comment = comment.strip()
    if len(comment) < MIN_COMMENT_LEN:
        return False, f"한 줄 피드백을 {MIN_COMMENT_LEN}글자 이상 써 주세요."
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO evaluations(round, presenter_sid, presenter_name, evaluator_sid,
                                    evaluator_name, scores, comment, updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(round, presenter_sid, evaluator_sid) DO UPDATE SET
                scores=excluded.scores, comment=excluded.comment,
                updated_at=excluded.updated_at, presenter_name=excluded.presenter_name,
                evaluator_name=excluded.evaluator_name
            """,
            (
                rnd,
                presenter["sid"],
                presenter["name"],
                evaluator["sid"],
                evaluator["name"],
                json.dumps(scores, ensure_ascii=False),
                comment[:MAX_COMMENT_LEN],
                now_str(),
            ),
        )
    return True, "제출했어요."


def _rows(sql, params=()):
    with get_conn() as conn:
        out = []
        for r in conn.execute(sql, params):
            d = dict(r)
            d["scores"] = json.loads(d["scores"])
            out.append(d)
        return out


def get_evaluation(rnd, presenter_sid, evaluator_sid):
    rows = _rows(
        "SELECT * FROM evaluations WHERE round=? AND presenter_sid=? AND evaluator_sid=?",
        (rnd, presenter_sid, evaluator_sid),
    )
    return rows[0] if rows else None


def evaluations_by_evaluator(evaluator_sid):
    return _rows(
        "SELECT * FROM evaluations WHERE evaluator_sid=? ORDER BY updated_at DESC",
        (evaluator_sid,),
    )


def evaluations_for_presenter(presenter_sid, rnd):
    return _rows(
        "SELECT * FROM evaluations WHERE presenter_sid=? AND round=? ORDER BY updated_at",
        (presenter_sid, rnd),
    )


def all_evaluations(rnd=None):
    if rnd is None:
        return _rows("SELECT * FROM evaluations ORDER BY round, presenter_sid, evaluator_sid")
    return _rows(
        "SELECT * FROM evaluations WHERE round=? ORDER BY presenter_sid, evaluator_sid",
        (rnd,),
    )


# ── 성찰 ──────────────────────────────────────
def save_reflection(rnd, sid, text):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO reflections(round, sid, text, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(round, sid) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
            (rnd, sid, text.strip(), now_str()),
        )


def get_reflection(rnd, sid):
    with get_conn() as conn:
        r = conn.execute(
            "SELECT text FROM reflections WHERE round=? AND sid=?", (rnd, sid)
        ).fetchone()
    return r["text"] if r else ""


def all_reflections(rnd):
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                """SELECT r.sid, COALESCE(s.name, '') AS name, r.text, r.updated_at
                   FROM reflections r LEFT JOIN students s ON r.sid = s.sid
                   WHERE r.round=? ORDER BY r.sid""",
                (rnd,),
            )
        ]


# ── 결과 정리·내보내기 ─────────────────────────
def total_of(scores):
    return sum(v for v in scores.values() if isinstance(v, (int, float)))


def evaluations_table(rows, rubric_names, with_evaluator=True):
    records = []
    for e in rows:
        rec = {
            "회차": e["round"],
            "발표자 학번": e["presenter_sid"],
            "발표자 이름": e["presenter_name"],
        }
        if with_evaluator:
            rec["평가자 학번"] = e["evaluator_sid"]
            rec["평가자 이름"] = e["evaluator_name"]
        for n in rubric_names:
            rec[n] = e["scores"].get(n)
        rec["총점"] = total_of(e["scores"])
        rec["한 줄 피드백"] = e["comment"]
        rec["제출 시각"] = e["updated_at"]
        records.append(rec)
    return pd.DataFrame(records)


def summary_table(rnd, rubric_names):
    rows = all_evaluations(rnd)
    presenters = list_presenters(rnd)
    records = []
    for p in presenters:
        mine = [e for e in rows if e["presenter_sid"] == p["sid"]]
        rec = {"순서": p["ord"] + 1, "학번": p["sid"], "이름": p["name"], "상태": p["status"],
               "평가 수": len(mine)}
        for n in rubric_names:
            vals = [e["scores"].get(n) for e in mine if e["scores"].get(n) is not None]
            rec[n] = round(sum(vals) / len(vals), 2) if vals else None
        rec["총점 평균"] = (
            round(sum(total_of(e["scores"]) for e in mine) / len(mine), 2) if mine else None
        )
        records.append(rec)
    return pd.DataFrame(records)


def export_backup():
    with get_conn() as conn:
        data = {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            for table in ["settings", "students", "presenters", "evaluations", "reflections"]
        }
    data["_exported_at"] = now_str()
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def restore_backup(raw):
    data = json.loads(raw.decode("utf-8"))
    tables = ["settings", "students", "presenters", "evaluations", "reflections"]
    if not all(t in data for t in tables):
        raise ValueError("이 앱에서 내려받은 백업 파일이 아니에요.")
    with get_conn() as conn:
        for t in tables:
            allowed = {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}
            for row in data[t]:
                if not set(row) <= allowed:
                    raise ValueError("백업 파일 형식이 맞지 않아요.")
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
            for row in data[t]:
                cols = ", ".join(row.keys())
                marks = ", ".join("?" for _ in row)
                conn.execute(f"INSERT INTO {t} ({cols}) VALUES ({marks})", tuple(row.values()))


def reset_all():
    with get_conn() as conn:
        for t in ["settings", "students", "presenters", "evaluations", "reflections"]:
            conn.execute(f"DELETE FROM {t}")
    init_db()


def get_teacher_password():
    try:
        return str(st.secrets["TEACHER_PASSWORD"])
    except Exception:
        return DEFAULT_TEACHER_PASSWORD


# ─────────────────────────────────────────────
# 화면 꾸미기
# ─────────────────────────────────────────────
CSS = """
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
html, body, [class*="css"], .stMarkdown, .stTextInput, .stButton, .stRadio, .stTabs {
    font-family: "Pretendard", -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
}
#MainMenu, footer, [data-testid="stToolbar"] {visibility: hidden;}
.block-container {padding-top: 2.2rem; padding-bottom: 4rem; max-width: 760px;}

.app-title {font-size: 1.65rem; font-weight: 800; letter-spacing: -0.02em; margin: 0;}
.app-sub {opacity: .65; margin: .15rem 0 1.2rem 0; font-size: .95rem;}

.card {
    border: 1px solid rgba(120,130,150,.25);
    background: rgba(43,92,170,.05);
    border-radius: 14px;
    padding: 14px 16px;
    margin: 8px 0;
}
.card .meta {font-size: .82rem; opacity: .6;}
.card .title {font-weight: 700; font-size: 1.02rem;}
.card .comment {margin-top: 6px; line-height: 1.55;}
.pill {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: .78rem; font-weight: 600; margin-right: 4px;
    background: rgba(43,92,170,.12);
}
.crit-name {font-weight: 700; margin-top: .6rem;}
.crit-desc {font-size: .86rem; opacity: .7; margin-bottom: -.4rem;}
.who {font-size: .92rem; opacity: .75;}

button[kind="primary"], [data-testid="stBaseButton-primary"],
[data-testid="stFormSubmitButton"] button {
    background: #2B5CAA !important; border-color: #2B5CAA !important; color: #fff !important;
}
</style>
"""


def esc(s):
    return html.escape(str(s))


def card(title, body="", meta="", pills=()):
    pill_html = "".join(f"<span class='pill'>{esc(p)}</span>" for p in pills)
    st.markdown(
        f"""<div class="card">
              <div class="meta">{esc(meta)}</div>
              <div class="title">{esc(title)}</div>
              <div style="margin-top:4px">{pill_html}</div>
              {'<div class="comment">' + esc(body) + '</div>' if body else ''}
            </div>""",
        unsafe_allow_html=True,
    )


def header():
    st.markdown(
        "<p class='app-title'>🎤 발표 동료평가</p>"
        "<p class='app-sub'>친구의 발표를 듣고 기준에 따라 점수와 한 줄 피드백을 남겨요.</p>",
        unsafe_allow_html=True,
    )


def logout_bar(text):
    c1, c2 = st.columns([4, 1])
    c1.markdown(f"<div class='who'>{esc(text)}</div>", unsafe_allow_html=True)
    if c2.button("로그아웃", use_container_width=True):
        st.session_state.pop("user", None)
        st.rerun()


# ─────────────────────────────────────────────
# 로그인 화면
# ─────────────────────────────────────────────
def login_view():
    tab_s, tab_t = st.tabs(["학생", "선생님"])
    with tab_s:
        with st.form("student_login"):
            sid = st.text_input("학번", placeholder="예: 20501")
            name = st.text_input("이름", placeholder="예: 김민지")
            pw = st.text_input("비밀번호 (숫자 4자리)", type="password", max_chars=4)
            st.caption("처음 들어오면 지금 입력한 비밀번호가 내 비밀번호로 등록돼요.")
            if st.form_submit_button("들어가기", use_container_width=True):
                ok, msg = login_student(sid, name, pw)
                if ok:
                    st.session_state.user = {"role": "student", "sid": sid.strip(),
                                             "name": name.strip()}
                    st.session_state.flash = msg
                    st.rerun()
                else:
                    st.error(msg)
    with tab_t:
        with st.form("teacher_login"):
            pw = st.text_input("선생님 비밀번호", type="password")
            if st.form_submit_button("들어가기", use_container_width=True):
                if pw == get_teacher_password():
                    st.session_state.user = {"role": "teacher"}
                    st.rerun()
                else:
                    st.error("비밀번호가 틀렸어요.")


# ─────────────────────────────────────────────
# 학생 화면
# ─────────────────────────────────────────────
def student_view(user):
    logout_bar(f"{user['sid']} {user['name']}님")
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))

    rnd = get_setting("current_round")
    rubric = get_setting("rubric", DEFAULT_RUBRIC)
    tab_eval, tab_mine, tab_recv = st.tabs(["✍️ 평가하기", "📋 내가 한 평가", "💬 내가 받은 피드백"])

    # ── 평가하기
    with tab_eval:
        st.markdown(f"**{esc(rnd)}**")
        open_ps = [
            p for p in list_presenters(rnd)
            if p["status"] == "평가중" and p["sid"] != user["sid"]
        ]
        if not open_ps:
            st.info("지금 평가할 수 있는 발표자가 없어요. 선생님이 평가를 열면 여기에 나타나요.")
            if st.button("새로고침", key="refresh_eval"):
                st.rerun()
        else:
            done = {
                e["presenter_sid"] for e in evaluations_by_evaluator(user["sid"])
                if e["round"] == rnd
            }
            labels = {
                p["sid"]: f"{p['name']} ({p['sid']})" + ("  ✅ 제출함" if p["sid"] in done else "")
                for p in open_ps
            }
            pid = st.selectbox("평가할 발표자", list(labels), format_func=labels.get)
            presenter = next(p for p in open_ps if p["sid"] == pid)
            existing = get_evaluation(rnd, pid, user["sid"])

            with st.form(f"eval_{rnd}_{pid}"):
                picks = {}
                for i, c in enumerate(rubric):
                    st.markdown(
                        f"<div class='crit-name'>{i+1}. {esc(c['name'])}</div>"
                        f"<div class='crit-desc'>{esc(c['desc'])}</div>",
                        unsafe_allow_html=True,
                    )
                    prev = existing["scores"].get(c["name"]) if existing else None
                    opts = list(LEVELS)
                    picks[c["name"]] = st.radio(
                        c["name"], opts, format_func=lambda v: f"{LEVELS[v]} ({v})",
                        index=opts.index(prev) if prev in opts else None,
                        horizontal=True, label_visibility="collapsed",
                        key=f"r_{rnd}_{pid}_{i}",
                    )
                comment = st.text_input(
                    "한 줄 피드백",
                    value=existing["comment"] if existing else "",
                    max_chars=MAX_COMMENT_LEN,
                    placeholder="잘한 점 또는 더 좋아질 점을 구체적으로 한 문장으로 써 주세요.",
                )
                submitted = st.form_submit_button(
                    "수정해서 다시 제출" if existing else "제출하기",
                    use_container_width=True,
                )
            if submitted:
                if any(v is None for v in picks.values()):
                    st.error("모든 기준에 점수를 골라 주세요.")
                else:
                    ok, msg = save_evaluation(rnd, presenter, user, picks, comment)
                    (st.success if ok else st.error)(msg)
                    if ok:
                        st.caption("제출한 평가는 '내가 한 평가'에서 확인할 수 있어요.")

    # ── 내가 한 평가 (본인 것만)
    with tab_mine:
        st.caption("내가 남긴 평가는 나와 선생님만 볼 수 있어요.")
        mine = evaluations_by_evaluator(user["sid"])
        if not mine:
            st.info("아직 남긴 평가가 없어요.")
        max_total = 3 * len(rubric)
        for e in mine:
            pills = [f"{k} {LEVELS.get(v, v)}" for k, v in e["scores"].items()]
            card(
                f"{e['presenter_name']} · 총점 {total_of(e['scores'])}/{max_total}",
                e["comment"],
                meta=f"{e['round']} · {e['updated_at']}",
                pills=pills,
            )

    # ── 내가 받은 피드백
    with tab_recv:
        open_rounds = get_setting("open_rounds", [])
        rounds = [r for r in get_setting("rounds", []) if r in open_rounds]
        if not rounds:
            st.info("선생님이 결과를 공개하면 여기에서 볼 수 있어요.")
        for r in rounds:
            received = evaluations_for_presenter(user["sid"], r)
            st.markdown(f"#### {esc(r)}")
            if not received:
                st.caption("이 회차에 받은 평가가 없어요.")
                continue
            st.caption(f"친구 {len(received)}명이 평가했어요. 누가 썼는지는 보이지 않아요.")
            cols = st.columns(len(rubric))
            for col, c in zip(cols, rubric):
                vals = [e["scores"].get(c["name"]) for e in received
                        if e["scores"].get(c["name"]) is not None]
                avg = sum(vals) / len(vals) if vals else 0
                col.metric(c["name"], f"{avg:.1f} / 3")
            st.markdown("**친구들의 한 줄 피드백**")
            for e in received:
                st.markdown(
                    f"<div class='card'><div class='comment'>💬 {esc(e['comment'])}</div></div>",
                    unsafe_allow_html=True,
                )
            st.markdown("**나의 성찰**")
            with st.form(f"reflect_{r}"):
                text = st.text_area(
                    "성찰",
                    value=get_reflection(r, user["sid"]),
                    placeholder="피드백을 읽고, 다음 발표에서 꼭 고치고 싶은 점 한 가지와 그 방법을 써 보세요.",
                    label_visibility="collapsed",
                    height=110,
                )
                if st.form_submit_button("성찰 저장", use_container_width=True):
                    save_reflection(r, user["sid"], text)
                    st.success("저장했어요.")


# ─────────────────────────────────────────────
# 선생님 화면
# ─────────────────────────────────────────────
def _on_status_change(rnd, sid, key):
    set_presenter_status(rnd, sid, st.session_state[key])


def _on_open_toggle(rnd, key):
    opened = set(get_setting("open_rounds", []))
    if st.session_state[key]:
        opened.add(rnd)
    else:
        opened.discard(rnd)
    set_setting("open_rounds", sorted(opened))


def teacher_view():
    logout_bar("선생님 모드")
    if st.session_state.get("flash"):
        st.info(st.session_state.pop("flash"))
    rubric = get_setting("rubric", DEFAULT_RUBRIC)
    names = [c["name"] for c in rubric]
    tab_run, tab_res, tab_set = st.tabs(["▶️ 진행", "📊 결과", "⚙️ 설정"])

    # ── 진행
    with tab_run:
        rounds = get_setting("rounds", [])
        cur = get_setting("current_round")
        c1, c2 = st.columns([3, 2])
        sel = c1.selectbox("진행할 회차", rounds, index=rounds.index(cur) if cur in rounds else 0)
        if sel != cur:
            set_setting("current_round", sel)
            st.rerun()
        with c2.popover("➕ 새 회차", use_container_width=True):
            new = st.text_input("회차 이름", placeholder="예: 2학년 5반 발표")
            if st.button("만들기", use_container_width=True):
                new = new.strip()
                if new and new not in rounds:
                    set_setting("rounds", rounds + [new])
                    set_setting("current_round", new)
                    st.rerun()
                else:
                    st.error("이름이 비었거나 이미 있는 회차예요.")
        rnd = get_setting("current_round")

        key = f"open_{rnd}"
        st.toggle(
            "학생에게 받은 결과 공개",
            value=rnd in get_setting("open_rounds", []),
            key=key,
            on_change=_on_open_toggle,
            args=(rnd, key),
            help="켜면 발표자가 '내가 받은 피드백'에서 이 회차 결과를 익명으로 볼 수 있어요.",
        )

        presenters = list_presenters(rnd)
        with st.expander("발표자 명단 입력", expanded=not presenters):
            st.caption("한 줄에 한 명씩 '학번 이름'으로 쓰세요. 엑셀에서 두 칸을 복사해 붙여도 돼요. 줄 순서가 발표 순서예요.")
            text = st.text_area(
                "명단",
                value="\n".join(f"{p['sid']} {p['name']}" for p in presenters),
                height=180,
                label_visibility="collapsed",
                placeholder="20501 김민지\n20502 박서준",
            )
            if st.button("명단 저장", type="primary"):
                ok, bad = parse_presenter_lines(text)
                set_presenters(rnd, ok)
                msg = f"{len(ok)}명을 저장했어요."
                if bad:
                    msg += " 형식이 맞지 않아 빠진 줄: " + ", ".join(bad)
                st.session_state.flash = msg
                st.rerun()

        if presenters:
            counts = {}
            for e in all_evaluations(rnd):
                counts[e["presenter_sid"]] = counts.get(e["presenter_sid"], 0) + 1
            st.markdown("**발표자별 평가 열기 / 마감**")
            st.caption("'평가중'인 발표자만 학생 화면에 나타나요. 여러 명을 동시에 열어도 돼요.")
            for p in presenters:
                c1, c2 = st.columns([2, 3])
                c1.markdown(
                    f"**{p['ord']+1}. {esc(p['name'])}**  \n"
                    f"<span class='who'>{esc(p['sid'])} · 평가 {counts.get(p['sid'], 0)}개</span>",
                    unsafe_allow_html=True,
                )
                k = f"st_{rnd}_{p['sid']}"
                c2.radio(
                    "상태", STATUSES, index=STATUSES.index(p["status"]),
                    horizontal=True, key=k, label_visibility="collapsed",
                    on_change=_on_status_change, args=(rnd, p["sid"], k),
                )
            b1, b2 = st.columns(2)
            if b1.button("모두 마감", use_container_width=True):
                for p in presenters:
                    set_presenter_status(rnd, p["sid"], "마감")
                st.rerun()
            if b2.button("새로고침", use_container_width=True):
                st.rerun()

    # ── 결과
    with tab_res:
        rnd = get_setting("current_round")
        st.markdown(f"**{esc(rnd)}** 결과")
        summ = summary_table(rnd, names)
        if summ.empty:
            st.info("발표자 명단을 먼저 입력해 주세요.")
        else:
            st.dataframe(summ, hide_index=True, use_container_width=True)

        detail = evaluations_table(all_evaluations(rnd), names)
        with st.expander(f"평가 원자료 보기 ({len(detail)}건, 평가자 이름 포함)"):
            if detail.empty:
                st.caption("아직 평가가 없어요.")
            else:
                st.dataframe(detail.drop(columns=["회차"]), hide_index=True,
                             use_container_width=True)

        refl = all_reflections(rnd)
        with st.expander(f"학생 성찰 보기 ({len(refl)}건)"):
            if refl:
                st.dataframe(
                    pd.DataFrame(refl).rename(columns={"sid": "학번", "name": "이름",
                                                       "text": "성찰", "updated_at": "저장 시각"}),
                    hide_index=True, use_container_width=True,
                )
            else:
                st.caption("아직 성찰이 없어요.")

        st.markdown("**내려받기**")
        d1, d2 = st.columns(2)
        d1.download_button(
            "이번 회차 결과 (엑셀용 CSV)",
            detail.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"동료평가_{rnd}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        all_detail = evaluations_table(all_evaluations(), names)
        d2.download_button(
            "전체 회차 결과 (엑셀용 CSV)",
            all_detail.to_csv(index=False).encode("utf-8-sig"),
            file_name="동료평가_전체.csv",
            mime="text/csv",
            use_container_width=True,
        )
        st.download_button(
            "🗂️ 전체 백업 파일 (.json) — 수업이 끝나면 꼭 받아 두세요",
            export_backup(),
            file_name=f"동료평가_백업_{datetime.now(KST).strftime('%m%d_%H%M')}.json",
            mime="application/json",
            use_container_width=True,
        )
        st.caption("무료 Streamlit Cloud는 앱이 오래 쉬거나 코드를 고치면 저장된 기록이 지워질 수 있어요. "
                   "백업 파일을 받아 두었다가 '설정 → 백업 복원'으로 되살리면 돼요.")

    # ── 설정
    with tab_set:
        if get_teacher_password() == DEFAULT_TEACHER_PASSWORD:
            st.warning("선생님 비밀번호가 기본값이에요. Streamlit Cloud의 Secrets에 "
                       "TEACHER_PASSWORD를 넣어 바꿔 주세요.")

        st.markdown("**채점기준**")
        st.caption("기준 이름과 설명을 고칠 수 있어요. 점수는 잘함 3 · 보통 2 · 노력 필요 1이에요.")
        edited = st.data_editor(
            pd.DataFrame(rubric).rename(columns={"name": "기준", "desc": "설명"}),
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            key="rubric_editor",
        )
        if st.button("채점기준 저장", type="primary"):
            new_rubric = [
                {"name": str(r["기준"]).strip(), "desc": str(r["설명"] or "").strip()}
                for _, r in edited.iterrows()
                if pd.notna(r["기준"]) and str(r["기준"]).strip()
            ]
            if not new_rubric:
                st.error("기준이 하나 이상 있어야 해요.")
            elif len({c["name"] for c in new_rubric}) != len(new_rubric):
                st.error("같은 이름의 기준이 있어요.")
            else:
                set_setting("rubric", new_rubric)
                st.success("저장했어요.")
                st.rerun()

        st.divider()
        st.markdown("**학생 비밀번호 초기화**")
        students = list_students()
        if students:
            s = st.selectbox("학생", [x["sid"] for x in students],
                             format_func=lambda sid: next(f"{x['sid']} {x['name']}"
                                                          for x in students if x["sid"] == sid))
            if st.button("초기화"):
                reset_student(s)
                st.success("초기화했어요. 학생이 다음에 들어올 때 새 비밀번호를 정하면 돼요.")
        else:
            st.caption("아직 로그인한 학생이 없어요.")

        st.divider()
        st.markdown("**백업 복원**")
        up = st.file_uploader("백업 파일(.json)", type=["json"])
        if up is not None and st.button("이 파일로 복원하기"):
            try:
                restore_backup(up.getvalue())
                st.success("복원했어요.")
                st.rerun()
            except Exception as ex:
                st.error(f"복원하지 못했어요: {ex}")

        st.divider()
        st.markdown("**전체 초기화**")
        sure = st.checkbox("모든 회차, 학생 계정, 평가 기록을 지우는 것에 동의해요.")
        if st.button("전체 초기화", disabled=not sure):
            reset_all()
            st.success("초기화했어요.")
            st.rerun()


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────
def main():
    st.set_page_config(page_title="발표 동료평가", page_icon="🎤", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    init_db()
    header()
    user = st.session_state.get("user")
    if user is None:
        login_view()
    elif user["role"] == "teacher":
        teacher_view()
    else:
        student_view(user)


if __name__ == "__main__":
    main()
