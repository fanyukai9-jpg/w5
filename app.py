"""台灣空氣品質儀表板 v2（互動加強版）
資料：aqx_p_488.csv（環境部 AQI 歷史測站資料，每小時）
執行：streamlit run app.py
"""
import time
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    import pydeck as pdk
except ImportError:  # 沒有 pydeck 就退回 st.map（少了懸停提示）
    pdk = None

CSV_PATH = str(Path(__file__).resolve().parent / "aqx_p_488.csv")
st.set_page_config(page_title="台灣空氣品質儀表板", page_icon="🌫️", layout="wide")

ALIASES = {
    "pm2.5": "pm25", "pm2_5": "pm25", "pm2.5_avg": "pm25_avg", "pm2_5_avg": "pm25_avg",
    "windspeed": "wind_speed", "winddirec": "wind_direc", "datacreationdate": "publishtime",
}
REQUIRED = ["sitename", "county", "aqi", "latitude", "longitude", "publishtime"]
NUMERIC = ["aqi", "pm25", "pm10", "o3", "no2", "so2", "co", "wind_speed", "latitude", "longitude"]
LABELS = {"aqi": "AQI", "pm25": "PM2.5", "pm10": "PM10", "o3": "臭氧 O₃", "no2": "二氧化氮 NO₂",
          "so2": "二氧化硫 SO₂", "co": "一氧化碳 CO", "wind_speed": "風速"}
REGIONS = {
    "北部": ["基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣"],
    "中部": ["苗栗縣", "臺中市", "彰化縣", "南投縣", "雲林縣"],
    "南部": ["嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣"],
    "東部": ["宜蘭縣", "花蓮縣", "臺東縣"],
    "離島": ["澎湖縣", "金門縣", "連江縣"],
}
COUNTY_TO_REGION = {c: r for r, cs in REGIONS.items() for c in cs}
STATUS_ORDER = ["良好", "普通", "對敏感族群不健康", "對所有族群不健康", "非常不健康", "危害"]
AQI_STEPS = [(50, (0, 176, 80)), (100, (255, 212, 0)), (150, (255, 126, 0)),
             (200, (230, 0, 0)), (300, (143, 63, 151))]
GRAY, MAROON = (153, 153, 153), (126, 0, 35)


# ---------- 資料 ----------
@st.cache_data
def load(path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "big5", "cp950"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("無法判斷檔案編碼")
    df.columns = [ALIASES.get(c.strip().lower(), c.strip().lower()) for c in df.columns]
    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "publishtime" in df.columns:
        df["publishtime"] = pd.to_datetime(df["publishtime"], errors="coerce")

    # 某些歷史匯出檔可能沒有 status / pollutant；據 AQI 補出可用欄位。
    if "status" not in df.columns:
        df["status"] = pd.cut(
            df["aqi"],
            bins=[-float("inf"), 50, 100, 150, 200, 300, float("inf")],
            labels=STATUS_ORDER,
            include_lowest=True,
        ).astype("object")
    if "pollutant" not in df.columns:
        candidates = [c for c in ("pm25", "pm10", "o3", "no2", "so2", "co") if c in df.columns]
        def _pollutant(row):
            if pd.isna(row["aqi"]):
                return None
            if not candidates:
                return None
            available = row[candidates].dropna()
            return available.idxmax() if not available.empty else None
        df["pollutant"] = df.apply(_pollutant, axis=1)

    df["region"] = df["county"].map(COUNTY_TO_REGION).fillna("其他")
    df["station"] = df["county"] + " " + df["sitename"]
    return df


def rgb_for(metric: str, v: float, lo: float, hi: float):
    if pd.isna(v):
        return GRAY
    if metric == "aqi":
        for limit, color in AQI_STEPS:
            if v <= limit:
                return color
        return MAROON
    t = 0.0 if hi == lo else min(1, max(0, (v - lo) / (hi - lo)))
    return (int(255 * min(1, 2 * t)), int(255 * min(1, 2 * (1 - t))), 40)


def advice(v: float) -> str:
    if pd.isna(v):
        return "目前沒有 AQI 數值。"
    if v <= 50:
        return "空氣品質良好，適合正常戶外活動。"
    if v <= 100:
        return "空氣品質普通，極少數非常敏感的人可能略受影響。"
    if v <= 150:
        return "敏感族群（氣喘、心肺疾病患者、老人與孩童）應減少長時間或劇烈的戶外活動。"
    if v <= 200:
        return "一般民眾也應減少戶外活動，敏感族群建議避免外出，外出可考慮配戴口罩。"
    return "空氣品質很差，建議留在室內並減少戶外活動。"


def draw_map(d: pd.DataFrame, metric: str, lo: float, hi: float, slot):
    d = d.dropna(subset=["latitude", "longitude"]).copy()
    rgb = [rgb_for(metric, v, lo, hi) for v in d[metric]]
    d["rgba"] = [[*c, 210] for c in rgb]
    d["hex"] = ["#%02x%02x%02x" % c for c in rgb]
    d["val"] = d[metric].round(1).astype(object).where(d[metric].notna(), "無資料")
    if pdk is None:
        slot.map(d, latitude="latitude", longitude="longitude", color="hex", size=2500)
        return
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=d[["sitename", "county", "status", "latitude", "longitude", "rgba", "val"]],
        get_position="[longitude, latitude]", get_fill_color="rgba",
        get_radius=2500, radius_min_pixels=5, radius_max_pixels=16, pickable=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(latitude=23.7, longitude=121.0, zoom=6.4),
        tooltip={"html": f"<b>{{county}} {{sitename}}</b><br/>{LABELS[metric]}：{{val}}<br/>{{status}}"},
    )
    slot.pydeck_chart(deck)


try:
    raw = load(CSV_PATH)
except FileNotFoundError:
    st.error(f"找不到 {CSV_PATH}，請把它放在和這支程式同一個資料夾。")
    st.stop()

missing = [c for c in REQUIRED if c not in raw.columns]
if missing:
    st.error(f"檔案缺少這些欄位：{missing}")
    st.write("你的檔案實際欄位：", list(raw.columns))
    st.stop()

metrics = [m for m in LABELS if m in raw.columns]
if not metrics:
    st.error("檔案中沒有可用的污染物指標欄位。")
    st.stop()

times = sorted(raw["publishtime"].dropna().unique())
if not times:
    st.error("檔案中沒有可用的發布時間。")
    st.stop()
times = [pd.Timestamp(t) for t in times]

# ---------- 側邊欄 ----------
st.sidebar.title("🌫️ 互動控制")
regions_all = [r for r in [*REGIONS, "其他"] if r in raw["region"].unique()]
region_pick = st.sidebar.multiselect("區域", regions_all, default=regions_all)
county_opts = sorted(raw[raw["region"].isin(region_pick)]["county"].unique())
county_pick = st.sidebar.multiselect("縣市", county_opts, default=county_opts)
metric = st.sidebar.selectbox("地圖／排名指標", metrics, format_func=LABELS.get)
t_sel = st.sidebar.select_slider("時間", options=times, value=times[-1],
                                 format_func=lambda t: f"{t:%m/%d %H:%M}")
aqi_max = int(raw["aqi"].dropna().max()) if raw["aqi"].notna().any() else 0
aqi_min = st.sidebar.slider("只顯示 AQI ≥", 0, max(0, aqi_max), 0)
status_opts = [s for s in STATUS_ORDER if s in raw["status"].dropna().unique()] or sorted(raw["status"].dropna().unique())
status_pick = st.sidebar.multiselect("空氣品質狀態", status_opts, default=status_opts)

base = raw[raw["county"].isin(county_pick)]
keep = base["aqi"].ge(aqi_min) & base["status"].isin(status_pick)
filtered = base[keep]                                   # 套用全部篩選（所有時間）
snap = filtered[filtered["publishtime"] == t_sel]        # 目前選的時間
lo, hi = raw[metric].min(), raw[metric].max()

# ---------- 標題與指標 ----------
st.title("台灣空氣品質儀表板")
st.caption(f"目前時間：{t_sel:%Y-%m-%d %H:%M}　符合條件的測站：{len(snap)} / {base[base['publishtime'] == t_sel].shape[0]}")
if snap.empty:
    st.warning("目前篩選條件下沒有測站，請放寬側邊欄的條件。")
    st.stop()

idx = times.index(t_sel)
prev = filtered[filtered["publishtime"] == times[idx - 1]]["aqi"].mean() if idx > 0 else None
cur = snap["aqi"].mean()
worst = snap.loc[snap["aqi"].idxmax()] if snap["aqi"].notna().any() else snap.iloc[0]
k1, k2, k3, k4 = st.columns(4)
k1.metric("平均 AQI", f"{cur:.0f}" if pd.notna(cur) else "—", None if prev is None or pd.isna(prev) else f"{cur - prev:+.1f}（較前一小時）",
          delta_color="inverse")
k2.metric("最高 AQI 測站", f"{worst['sitename']}（{worst['aqi']:.0f}）")
k3.metric("AQI > 100 測站", int((snap["aqi"] > 100).sum()))
k4.metric("空氣良好比例", f"{(snap['aqi'] <= 50).mean() * 100:.0f}%")

tab_map, tab_trend, tab_cmp, tab_station, tab_data = st.tabs(
    ["🗺️ 地圖", "📈 趨勢", "📊 比較", "📍 單一測站", "📥 資料"])

# ---------- 地圖 ----------
with tab_map:
    st.caption("顏色：AQI 用官方等級色（綠、黃、橘、紅、紫）；其他指標用綠→紅漸層。滑鼠移到點上可看數值。")
    play = st.button("▶ 播放這段時間的地圖動畫")
    cap = st.empty()
    slot = st.empty()
    if play:
        for t in times:
            frame = filtered[filtered["publishtime"] == t]
            cap.markdown(f"**{t:%m/%d %H:%M}**　{LABELS[metric]}　測站 {len(frame)}")
            draw_map(frame, metric, lo, hi, slot)
            time.sleep(0.8)
    else:
        draw_map(snap, metric, lo, hi, slot)

# ---------- 趨勢 ----------
with tab_trend:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("測站比較（折線圖）")
        top3 = snap.nlargest(3, "aqi")["station"].tolist()
        picks = st.multiselect("選擇測站（可多選）", sorted(filtered["station"].unique()), default=top3)
        m1 = st.selectbox("指標", metrics, format_func=LABELS.get, key="trend_metric")
        if picks:
            line = filtered[filtered["station"].isin(picks)].pivot_table(
                index="publishtime", columns="station", values=m1, aggfunc="mean")
            st.line_chart(line)
        else:
            st.info("請至少選一個測站。")
    with c2:
        st.subheader("分組堆疊（面積圖）")
        by = st.radio("分組方式", ["縣市", "區域"], horizontal=True)
        col = "county" if by == "縣市" else "region"
        m2 = st.selectbox("指標", metrics, index=metrics.index("pm25") if "pm25" in metrics else 0,
                          format_func=LABELS.get, key="area_metric")
        st.area_chart(filtered.pivot_table(index="publishtime", columns=col, values=m2, aggfunc="mean").fillna(0))

# ---------- 比較 ----------
with tab_cmp:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader(f"{LABELS[metric]} 排名（長條圖）")
        level = st.radio("比較單位", ["縣市", "區域", "測站"], horizontal=True)
        col = {"縣市": "county", "區域": "region", "測站": "station"}[level]
        order = st.radio("排序", ["高 → 低", "低 → 高"], horizontal=True)
        rank = snap.groupby(col)[metric].mean().sort_values(ascending=(order == "低 → 高"))
        n = st.slider("顯示前幾名", 1, max(1, len(rank)), min(10, len(rank)))
        st.bar_chart(rank.head(n))
    with c2:
        st.subheader("兩個變數的關係（散佈圖）")
        opts = [m for m in metrics]
        x = st.selectbox("X 軸", opts, index=opts.index("wind_speed") if "wind_speed" in opts else 0,
                         format_func=LABELS.get)
        y = st.selectbox("Y 軸", opts, index=opts.index("pm25") if "pm25" in opts else 0,
                         format_func=LABELS.get, key="y_axis")
        color_by = st.selectbox("顏色分組", ["county", "region", "status"],
                                format_func={"county": "縣市", "region": "區域", "status": "狀態"}.get)
        pts = snap.dropna(subset=[x, y])
        if len(pts) > 2 and x != y:
            st.caption(f"相關係數 r = {pts[[x, y]].corr().iloc[0, 1]:.2f}（{len(pts)} 個測站）")
        st.scatter_chart(pts, x=x, y=y, color=color_by)
    c3, c4 = st.columns(2)
    with c3:
        st.subheader("空氣品質狀態分布")
        st.bar_chart(snap["status"].value_counts().reindex(status_opts).dropna())
    with c4:
        st.subheader("主要污染物（AQI 偏高時）")
        pol = snap["pollutant"].dropna().value_counts()
        if pol.empty:
            st.info("這個時間點沒有測站標示主要污染物（代表空氣品質良好）。")
        else:
            st.bar_chart(pol)

# ---------- 單一測站 ----------
with tab_station:
    names = sorted(base["station"].unique())
    default = names.index(worst["station"]) if worst["station"] in names else 0
    s = st.selectbox("選擇測站", names, index=default)
    one = base[base["station"] == s].sort_values("publishtime")
    now = one[one["publishtime"] == t_sel]
    if now.empty:
        st.warning("這個測站在所選時間沒有資料。")
    else:
        r = now.iloc[0]
        a, b, c, d = st.columns(4)
        a.metric("AQI", "—" if pd.isna(r["aqi"]) else f"{r['aqi']:.0f}")
        b.metric("狀態", r["status"] if pd.notna(r["status"]) else "—")
        c.metric("主要污染物", r["pollutant"] if pd.notna(r.get("pollutant")) else "無")
        d.metric("PM2.5", "—" if pd.isna(r.get("pm25")) else f"{r['pm25']:.0f}")
        st.info(advice(r["aqi"]))
    show = st.multiselect("要畫的指標", metrics, default=[m for m in ["aqi", "pm25"] if m in metrics],
                          format_func=LABELS.get)
    if show:
        st.line_chart(one.set_index("publishtime")[show].rename(columns=LABELS))

# ---------- 資料 ----------
with tab_data:
    st.caption("表格可點欄位排序，右上角有搜尋與全螢幕。")
    cols = [c for c in ["publishtime", "region", "county", "sitename", "aqi", "status", "pollutant",
                        "pm25", "pm10", "o3", "no2", "wind_speed", "latitude", "longitude"] if c in snap.columns]
    st.dataframe(snap[cols].sort_values("aqi", ascending=False), hide_index=True)
    d1, d2 = st.columns(2)
    d1.download_button("⬇ 下載目前時間點（CSV）", snap[cols].to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"aqi_{t_sel:%Y%m%d_%H%M}.csv", mime="text/csv")
    d2.download_button("⬇ 下載全部篩選結果（CSV）", filtered.to_csv(index=False).encode("utf-8-sig"),
                       file_name="aqi_filtered.csv", mime="text/csv")