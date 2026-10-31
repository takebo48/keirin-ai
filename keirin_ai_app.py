import itertools
import re
from io import StringIO
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="競輪AIアプリ", page_icon="🚴", layout="wide")
st.title("🚴 競輪AIアプリ")
st.caption("出走表の取得または手入力をもとに買い目候補を整理します。的中を保証するものではありません。")
tab_predict, tab_backtest = st.tabs(["予想", "バックテスト"])

def txt(v):
    return "" if pd.isna(v) else str(v).replace("\u3000", " ").strip()

def flat_cols(cols):
    out = []
    for c in cols:
        if isinstance(c, tuple):
            out.append(" ".join(txt(x) for x in c if txt(x) and not str(x).startswith("Unnamed")).strip())
        else:
            out.append(txt(c))
    return out

def find_col(cols, terms):
    for term in terms:
        for col in cols:
            if term in str(col).replace(" ", "").replace("\n", ""):
                return col
    return None

def scrape_table(url):
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    best, best_score = None, -1
    for t in tables:
        t = t.copy()
        t.columns = flat_cols(t.columns)
        normalized = [str(c).replace(" ", "").replace("\n", "") for c in t.columns]
        score = sum(any(k in c for c in normalized) for k in ["車", "競走得点", "脚質"])
        if score > best_score:
            best, best_score = t, score
    if best is None or best_score < 2:
        raise ValueError("車番・競走得点・脚質の列が見つかりませんでした。")
    cols = list(best.columns)
    car = find_col(cols, ["車"])
    rating = find_col(cols, ["競走得点", "得点"])
    style = find_col(cols, ["脚質"])
    name = find_col(cols, ["選手名", "選手"])
    mark = find_col(cols, ["本紙", "印"])
    if car is None or rating is None:
        raise ValueError("車番または競走得点を読み取れませんでした。")
    out = pd.DataFrame()
    out["車番"] = pd.to_numeric(best[car].astype(str).str.extract(r"(\d+)")[0], errors="coerce")
    out["選手名"] = best[name].map(txt) if name else ""
    out["評価点"] = pd.to_numeric(best[rating].astype(str).str.replace(",", "", regex=False).str.extract(r"(\d+(?:\.\d+)?)")[0], errors="coerce")
    out["脚質"] = best[style].map(txt) if style else "両"
    out["印"] = best[mark].map(txt) if mark else ""
    out = out.dropna(subset=["車番", "評価点"])
    out["車番"] = out["車番"].astype(int)
    out = out[out["車番"].between(1, 9)].drop_duplicates("車番").sort_values("車番")
    if len(out) < 3:
        raise ValueError("選手データを3人以上読み取れませんでした。")
    return out.reset_index(drop=True)

def parse_lines(s):
    groups = []
    for part in re.split(r"[,、\n]+", s or ""):
        nums = [int(x) for x in re.findall(r"\d+", part)]
        nums = [n for n in nums if 1 <= n <= 9]
        if nums:
            groups.append(nums)
    return groups

def score_riders(df, mode, line_text):
    result = df.copy()
    result["評価点"] = pd.to_numeric(result["評価点"], errors="coerce").fillna(0)
    result["オッズ"] = pd.to_numeric(result.get("オッズ", 0), errors="coerce")
    if not isinstance(result["オッズ"], pd.Series):
        result["オッズ"] = 0.0
    style_bonus = {"逃": 3.0, "両": 2.0, "捲": 2.5, "差": 1.5, "追": 0.5}
    result["脚質点"] = result["脚質"].astype(str).map(lambda s: max([v for k, v in style_bonus.items() if k in s] or [0.0]))
    result["印点"] = result.get("印", pd.Series("", index=result.index)).astype(str).map(
        lambda s: 5 if "◎" in s else 3.5 if "○" in s else 2.5 if "▲" in s else 1 if "△" in s else 0
    )
    result["並び点"] = 0.0
    for group in parse_lines(line_text):
        for pos, car in enumerate(group):
            if car in result["車番"].values:
                result.loc[result["車番"] == car, "並び点"] += 2.0 if pos == 0 else 1.5 if pos == 1 else 1.0
    result["穴点"] = result["オッズ"].map(lambda x: min(float(x), 30) / 10 if mode == "荒れ（穴狙い）" and x > 0 else 0)
    result["AIスコア"] = result["評価点"] + result["脚質点"] + result["印点"] + result["並び点"] + result["穴点"]
    return result.sort_values(["AIスコア", "評価点"], ascending=False).reset_index(drop=True)

with tab_predict:
    st.subheader("1. レース情報")
    mode = st.radio("モード選択", ["普通（安定）", "荒れ（穴狙い）"], horizontal=True)
    count = st.selectbox("出走人数", [7, 9])
    url = st.text_input("出走表URL（任意）", placeholder="https://keirin.netkeiba.com/race/entry/?race_id=...", key="entry_url")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("出走表を取得", key="scrape_button"):
            if not url.strip():
                st.warning("先に出走表URLを入力してください。")
            else:
                try:
                    st.session_state["keirin_scraped_df"] = scrape_table(url.strip())
                    st.success(f"{len(st.session_state['keirin_scraped_df'])}人分を読み取りました。")
                except Exception as exc:
                    st.error("取得できませんでした。サイト側の制限の可能性があります。下の手入力で続けられます。")
                    st.caption(f"詳細：{exc}")
    with c2:
        if st.button("取得データを解除", key="clear_scraped"):
            st.session_state.pop("keirin_scraped_df", None)
            st.rerun()

    st.subheader("2. 選手データ")
    scraped = st.session_state.get("keirin_scraped_df")
    source = None
    if isinstance(scraped, pd.DataFrame) and len(scraped) >= 3:
        source = scraped.head(count)[["車番", "選手名", "評価点", "脚質", "印"]].copy()
        source["オッズ"] = 0.0
        st.info("取得した出走表を使っています。手入力に切り替えるには「取得データを解除」を押してください。")
    if source is None:
        st.caption("URL取得が失敗しても、下に入力すれば予想できます。オッズは任意です。")
        rows = []
        for i in range(1, count + 1):
            with st.expander(f"{i}番車", expanded=(i <= 3)):
                a, b, c = st.columns(3)
                rating = a.number_input("競走得点", min_value=0.0, max_value=150.0, value=85.0, step=0.1, key=f"rating_{i}")
                style = b.selectbox("脚質", ["逃", "両", "捲", "差", "追"], index=1, key=f"style_{i}")
                odds = c.number_input("オッズ（任意）", min_value=0.0, max_value=9999.0, value=0.0, step=0.1, key=f"odds_{i}")
                name = st.text_input("選手名（任意）", key=f"name_{i}")
                mark = st.selectbox("印（任意）", ["なし", "◎", "○", "▲", "△", "注"], key=f"mark_{i}")
                rows.append({"車番": i, "選手名": name, "評価点": rating, "脚質": style, "印": "" if mark == "なし" else mark, "オッズ": odds})
        source = pd.DataFrame(rows)

    lines = st.text_input("並び予想（例：1-6-7,2-4,3-5）", value="1-6-7,2-4,3-5", key="line_text")
    budget = st.number_input("購入予算（円）", min_value=100, max_value=100000, value=1000, step=100, key="budget")
    if st.button("予想を作成", type="primary", key="make_prediction"):
        st.session_state["keirin_last_ranked"] = score_riders(source, mode, lines)
        st.session_state["keirin_last_budget"] = int(budget)

    ranked = st.session_state.get("keirin_last_ranked")
    if isinstance(ranked, pd.DataFrame) and not ranked.empty:
        st.subheader("3. AI評価")
        cols = [c for c in ["車番", "選手名", "評価点", "脚質", "印", "並び点", "穴点", "AIスコア"] if c in ranked.columns]
        st.dataframe(ranked[cols], hide_index=True, use_container_width=True)
        cars = ranked["車番"].astype(int).tolist()
        if len(cars) >= 3:
            a, b, c = cars[:3]
            d = cars[3] if len(cars) > 3 else a
            e = cars[4] if len(cars) > 4 else b
            st.subheader("4. 買い目候補")
            st.markdown("**大本命（三連単）**")
            st.write(f"{a} → {b} → {c}")
            st.write(f"{a} → {c} → {b}")
            st.markdown("**中穴（三連単）**")
            st.write(f"{b} → {a} → {d}")
            st.write(f"{a} → {d} → {b}")
            st.markdown("**穴狙い（三連単）**")
            st.write(f"{d} → {e} → {a}")
            st.write(f"{e} → {a} → {d}")
            st.markdown("**二車単**")
            st.write(f"{a} → {b} ／ {b} → {a} ／ {a} → {c}")
            each = max(100, int(st.session_state.get("keirin_last_budget", 1000) / 6 // 100) * 100)
            st.write(f"購入額の目安：1点あたり{each:,}円（6点で{each * 6:,}円。予算に合わせて調整してください）")
            st.caption("候補は評価スコア順の組み合わせです。実際のオッズや直前情報を確認してください。的中保証はありません。")
        else:
            st.warning("予想には3人以上のデータが必要です。")

with tab_backtest:
    st.subheader("バックテスト")
    st.write("CSVは1行を1選手とし、`race_id`、`車番`、`評価点`、`脚質`、`result` の列を用意してください。`result` は1着の選手を1、それ以外を0にします。")
    st.caption("簡易検証です。三連単の的中率や実収支を再現する機能ではありません。")
    upload = st.file_uploader("バックテストCSV", type=["csv"], key="backtest_csv")
    if upload is not None:
        try:
            hist = pd.read_csv(upload)
            required = {"race_id", "車番", "評価点", "脚質", "result"}
            missing = required - set(hist.columns)
            if missing:
                st.error("CSVに必要な列がありません：" + "、".join(sorted(missing)))
            else:
                hist["result"] = pd.to_numeric(hist["result"], errors="coerce").fillna(0)
                summaries = []
                for race_id, group in hist.groupby("race_id"):
                    g = group.copy()
                    g["印"] = ""
                    g["オッズ"] = 0.0
                    scored = score_riders(g, "普通（安定）", "")
                    predicted = int(scored.iloc[0]["車番"])
                    winners = group.loc[group["result"] == 1, "車番"].tolist()
                    summaries.append({"race_id": race_id, "予測1位車番": predicted, "1着車番": ", ".join(map(str, winners)), "的中": predicted in winners})
                summary = pd.DataFrame(summaries)
                st.dataframe(summary, hide_index=True, use_container_width=True)
                if not summary.empty:
                    st.metric("1着予測の的中率", f"{summary['的中'].mean() * 100:.1f}%")
                    st.caption(f"検証レース数：{len(summary)}")
        except Exception as exc:
            st.error(f"CSVを読み込めませんでした：{exc}")
