"""臺灣空氣品質監測
資料：環境部 AQI 歷史測站資料。可切換本機 CSV 快照或即時 API；
API 失敗時會自動退回 CSV。
執行：streamlit run app.py
"""

import time
from datetime import datetime
import pandas as pd
import requests
import streamlit as st
import altair as alt
from streamlit_autorefresh import st_autorefresh

try:
    import pydeck as pdk
except ImportError:  # 沒有 pydeck 就退回 st.map
    pdk = None


CSV_PATH = "aqx_p_488.csv"
API_URL = "https://data.moenv.gov.tw/api/v2/aqx_p_488"

st.set_page_config(
    page_title="臺灣空氣品質監測｜即時與趨勢",
    page_icon="🌫️",
    layout="wide",
)

st.markdown(
    """
    <style>
    .mapboxgl-ctrl-zoom-in,
    .mapboxgl-ctrl-zoom-out {
        display: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

ALIASES = {
    "pm2.5": "pm25",
    "pm2_5": "pm25",
    "pm2.5_avg": "pm25_avg",
    "pm2_5_avg": "pm25_avg",
    "windspeed": "wind_speed",
    "winddirec": "wind_direc",
    "datacreationdate": "publishtime",
}

REQUIRED = ["sitename", "county", "aqi", "latitude", "longitude", "publishtime"]
NUMERIC = [
    "aqi", "pm25", "pm10", "o3", "no2", "so2", "co",
    "wind_speed", "latitude", "longitude",
]

LABELS = {
    "aqi": "AQI",
    "pm25": "PM2.5",
    "pm10": "PM10",
    "o3": "臭氧 O₃",
    "no2": "二氧化氮 NO₂",
    "so2": "二氧化硫 SO₂",
    "co": "一氧化碳 CO",
    "wind_speed": "風速",
}

REGIONS = {
    "北部": ["基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣"],
    "中部": ["苗栗縣", "臺中市", "彰化縣", "南投縣", "雲林縣"],
    "南部": ["嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣"],
    "東部": ["宜蘭縣", "花蓮縣", "臺東縣"],
    "離島": ["澎湖縣", "金門縣", "連江縣"],
}

COUNTY_TO_REGION = {
    county: region
    for region, counties in REGIONS.items()
    for county in counties
}

STATUS_ORDER = [
    "良好",
    "普通",
    "對敏感族群不健康",
    "對所有族群不健康",
    "非常不健康",
    "危害",
]

AQI_STEPS = [
    (50, (0, 176, 80)),
    (100, (255, 212, 0)),
    (150, (255, 126, 0)),
    (200, (230, 0, 0)),
    (300, (143, 63, 151)),
]

GRAY = (153, 153, 153)
MAROON = (126, 0, 35)

# ---------- 資料 ----------
def clean(df: pd.DataFrame) -> pd.DataFrame:
    """統一欄位名稱、轉數字與時間格式，並加上區域和測站標籤。"""
    df = df.copy()
    df.columns = [
        ALIASES.get(str(column).strip().lower(), str(column).strip().lower())
        for column in df.columns
    ]

    for column in NUMERIC:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    if "publishtime" in df.columns:
        df["publishtime"] = pd.to_datetime(
            df["publishtime"],
            errors="coerce",
        )

    if {"county", "sitename"} <= set(df.columns):
        df["region"] = df["county"].map(COUNTY_TO_REGION).fillna("其他")
        df["station"] = df["county"] + " " + df["sitename"]

    return df


@st.cache_data
def load_csv(path: str) -> pd.DataFrame:
    """讀取本機 CSV 快照。"""
    for encoding in ("utf-8-sig", "utf-8", "big5", "cp950"):
        try:
            return clean(pd.read_csv(path, encoding=encoding))
        except UnicodeDecodeError:
            continue

    raise ValueError("無法判斷檔案編碼")


@st.cache_data(ttl=30, show_spinner="向環境部抓取最新資料…")
def load_live(api_key: str):
    """抓取最近 1000 筆資料，並回傳資料與抓取時間。"""
    params = {
        "language": "zh",
        "offset": 0,
        "limit": 1000,
        "api_key": api_key,
    }
    response = requests.get(API_URL, params=params, timeout=20)

    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        if "API KEY" in body.upper() and ("不存在" in body or "失效" in body):
            raise RuntimeError(
                "環境部拒絕此 API 金鑰。請確認它是環境部資料開放平台核發的會員 API Key。"
            )
        content_type = response.headers.get("content-type", "未知格式")
        preview = body[:160] or "空白回應"
        raise RuntimeError(
            f"環境部回傳非 JSON 資料（HTTP {response.status_code}，"
            f"{content_type}）：{preview}"
        )

    if response.status_code != 200:
        raise RuntimeError(f"環境部 API 回傳 HTTP {response.status_code}：{payload}")

    if isinstance(payload, list):
        # aqx_p_488 currently returns the record list at the top level.
        records = payload
    else:
        records = None
    if records is None and isinstance(payload, dict):
        # MOENV FAQ documents records under result.records; accept the flat
        # and top-level list forms for API-version compatibility.
        result = payload.get("result")
        if isinstance(result, dict):
            records = result.get("records")
        if records is None:
            records = payload.get("records")
    if not records:
        detail = (
            payload.get("message")
            or payload.get("error")
            or payload.get("result")
            if isinstance(payload, dict)
            else payload
        )
        raise RuntimeError(f"API 沒有回傳資料：{str(detail)[:160]}")

    df = clean(pd.DataFrame(records))
    keys = [column for column in ("siteid", "publishtime") if column in df.columns]
    if keys:
        df = df.drop_duplicates(keys)

    return df, datetime.now()


def secret_key() -> str:
    """讀取 Streamlit secrets 裡的環境部 API 金鑰。"""
    try:
        return st.secrets.get("MOENV_API_KEY", "")
    except Exception:
        return ""


def rgb_for(metric: str, value: float, low: float, high: float):
    """依指標數值計算地圖標記顏色。"""
    if pd.isna(value):
        return GRAY

    if metric == "aqi":
        for limit, color in AQI_STEPS:
            if value <= limit:
                return color
        return MAROON

    ratio = 0.0 if high == low else min(
        1,
        max(0, (value - low) / (high - low)),
    )

    return (
        int(255 * min(1, 2 * ratio)),
        int(255 * min(1, 2 * (1 - ratio))),
        40,
    )


def advice(value: float) -> str:
    """依 AQI 提供簡短建議。"""
    if pd.isna(value):
        return "目前沒有 AQI 數值。"
    if value <= 50:
        return "空氣品質良好，適合正常戶外活動。"
    if value <= 100:
        return "空氣品質普通，極少數非常敏感的人可能略受影響。"
    if value <= 150:
        return (
            "敏感族群（氣喘、心肺疾病患者、老人與孩童）"
            "應減少長時間或劇烈的戶外活動。"
        )
    if value <= 200:
        return (
            "一般民眾也應減少戶外活動，敏感族群建議避免外出，"
            "外出可考慮配戴口罩。"
        )

    return "空氣品質很差，建議留在室內並減少戶外活動。"


def draw_map(
    data: pd.DataFrame,
    metric: str,
    low: float,
    high: float,
    slot,
    zoom: float = 6.4,
):
    """繪製測站地圖。"""
    data = data.dropna(subset=["latitude", "longitude"]).copy()

    colors = [rgb_for(metric, value, low, high) for value in data[metric]]

    data["rgba"] = [[*color, 210] for color in colors]
    data["hex"] = ["#%02x%02x%02x" % color for color in colors]
    data["val"] = (
        data[metric]
        .round(1)
        .astype(object)
        .where(data[metric].notna(), "無資料")
    )
    ratio = (
        (data[metric] - low).div(high - low).clip(0, 1).fillna(0.5)
        if high != low
        else pd.Series(0.5, index=data.index)
    )
    data["radius_m"] = 2500 + ratio * 6500
    data["size_m"] = 100 + ratio * 900

    if pdk is None:
        slot.map(
            data,
            latitude="latitude",
            longitude="longitude",
            color="hex",
            size="size_m",
            zoom=round(zoom),
        )
        return

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=data[
            [
                "sitename", "county", "status",
                "latitude", "longitude", "rgba", "val", "radius_m",
            ]
        ],
        get_position="[longitude, latitude]",
        get_fill_color="rgba",
        get_radius="radius_m",
        radius_min_pixels=5,
        radius_max_pixels=16,
        pickable=True,
    )

    deck = pdk.Deck(
        layers=[layer],
        views=[pdk.View(type="MapView", controller=False)],
        initial_view_state=pdk.ViewState(
            latitude=23.7,
            longitude=121.0,
            zoom=zoom,
        ),
        tooltip={
            "html": (
                f"<b>{{county}} {{sitename}}</b><br/>"
                f"{LABELS[metric]}：{{val}}<br/>{{status}}"
            )
        },
    )

    slot.pydeck_chart(deck)


# ---------- 側邊欄：資料來源與即時更新 ----------
st.sidebar.title("🌫️ 空品監測條件")
has_api_key = bool(secret_key())
source = st.sidebar.radio(
    "資料來源",
    ["本機 CSV 快照", "即時 API"],
    index=1 if has_api_key else 0,
)

raw = None
source_note = ""

if source == "即時 API":
    api_key = st.sidebar.text_input(
        "環境部 API 金鑰",
        type="password",
        value=secret_key(),
    )

    auto_refresh = st.sidebar.toggle("自動更新", value=True)
    refresh_seconds = st.sidebar.selectbox(
        "更新頻率",
        [30, 60, 120, 300],
        index=1,
        format_func=lambda seconds: f"每 {seconds} 秒",
    )

    if auto_refresh and api_key:
        st_autorefresh(
            interval=refresh_seconds * 1000,
            key="aqi_live_refresh",
        )

    follow_latest = st.sidebar.toggle("自動跟隨最新資料時間", value=True)

    if st.sidebar.button("🔄 重新抓取"):
        load_live.clear()

    if api_key:
        try:
            raw, fetched = load_live(api_key)
            source_note = f"即時 API（抓取時間 {fetched:%H:%M}）"
        except requests.exceptions.Timeout:
            st.sidebar.warning(
                "環境部 API 連線逾時，已改用本機 CSV。可稍後按「重新抓取」。"
            )
        except Exception as error:
            st.sidebar.warning(
                f"環境部 API 暫時無法使用，已改用本機 CSV。原因：{error}"
            )
    else:
        st.sidebar.info("尚未輸入金鑰，先使用本機 CSV。")


if raw is None:
    try:
        raw = load_csv(CSV_PATH)
        source_note = f"本機 CSV 快照（{CSV_PATH}）"
    except FileNotFoundError:
        st.error(f"找不到 {CSV_PATH}，請把它放在和這支程式同一個資料夾。")
        st.stop()


missing = [column for column in REQUIRED if column not in raw.columns]
if missing:
    st.error(f"檔案缺少這些欄位：{missing}")
    st.write("你的檔案實際欄位：", list(raw.columns))
    st.stop()


metrics = [metric for metric in LABELS if metric in raw.columns]
times = sorted(raw["publishtime"].dropna().unique())
times = [pd.Timestamp(value) for value in times]


# ---------- 側邊欄：篩選條件 ----------
regions_all = [
    region
    for region in [*REGIONS, "其他"]
    if region in raw["region"].unique()
]

region_pick = st.sidebar.multiselect(
    "區域",
    regions_all,
    default=regions_all,
)

county_opts = sorted(
    raw[raw["region"].isin(region_pick)]["county"].unique()
)

county_pick = st.sidebar.multiselect(
    "縣市",
    county_opts,
    default=county_opts,
)

metric = st.sidebar.selectbox(
    "地圖與排名指標",
    metrics,
    format_func=LABELS.get,
)

if source == "即時 API" and auto_refresh and follow_latest:
    t_sel = times[-1]
    st.sidebar.caption(f"目前顯示最新資料：{t_sel:%m/%d %H:%M}")
else:
    t_sel = st.sidebar.select_slider(
        "時間",
        options=times,
        value=times[-1],
        format_func=lambda value: f"{value:%m/%d %H:%M}",
    )

aqi_min = st.sidebar.slider(
    "只顯示 AQI ≥",
    0,
    int(raw["aqi"].max()),
    0,
)

status_opts = [
    status
    for status in STATUS_ORDER
    if status in raw["status"].unique()
] or sorted(raw["status"].dropna().unique())

status_pick = st.sidebar.multiselect(
    "空氣品質狀態",
    status_opts,
    default=status_opts,
)


base = raw[raw["county"].isin(county_pick)]
keep = base["aqi"].ge(aqi_min) & base["status"].isin(status_pick)
filtered = base[keep]
snap = filtered[filtered["publishtime"] == t_sel]

low, high = raw[metric].min(), raw[metric].max()


# ---------- 標題與指標 ----------
st.title("臺灣空氣品質監測")
st.caption(
    "即時掌握各地空氣品質，追蹤污染變化，並比較區域與測站。\n\n"
    f"資料來源：{source_note}｜"
    f"資料範圍：{times[0]:%m/%d %H:%M}–{times[-1]:%m/%d %H:%M}｜"
    f"目前檢視：{t_sel:%m/%d %H:%M}｜"
    f"符合條件的測站：{len(snap)} / "
    f"{base[base['publishtime'] == t_sel].shape[0]}"
)

if snap.empty:
    st.warning("目前篩選條件下沒有測站，請放寬側邊欄的條件。")
    st.stop()


idx = times.index(t_sel)
prev = (
    filtered[filtered["publishtime"] == times[idx - 1]]["aqi"].mean()
    if idx > 0
    else None
)

current_aqi = snap["aqi"].mean()
worst = snap.loc[snap["aqi"].idxmax()]

k1, k2, k3, k4 = st.columns(4)
k1.metric(
    "平均 AQI",
    f"{current_aqi:.0f}",
    None if prev is None or pd.isna(prev)
    else f"{current_aqi - prev:+.1f}（較前一小時）",
    delta_color="inverse",
)
k2.metric("最高測站 AQI", f"{worst['sitename']} {worst['aqi']:.0f}")
k3.metric("AQI > 100 測站", int((snap["aqi"] > 100).sum()))
k4.metric("空氣良好比例", f"{(snap['aqi'] <= 50).mean() * 100:.0f}%")


tab_map, tab_trend, tab_cmp, tab_station, tab_advanced, tab_data = st.tabs(
    ["即時概況", "時間趨勢", "區域比較", "測站分析", "時序熱圖", "資料明細"]
)


# ---------- 地圖 ----------
with tab_map:
    st.caption(
        f"測站位置與{LABELS[metric]}分布；點的顏色與大小都代表指標高低。"
        "移到測站點上可查看狀態與數值。"
    )

    play = st.button("▶ 播放這段時間的地圖動畫")
    caption_slot = st.empty()
    map_slot = st.empty()

    if play:
        for timestamp in times:
            frame = filtered[filtered["publishtime"] == timestamp]
            caption_slot.markdown(
                f"**{timestamp:%m/%d %H:%M}**　"
                f"{LABELS[metric]}　測站 {len(frame)}"
            )
            draw_map(
                frame, metric, low, high, map_slot,
            )
            time.sleep(0.8)
    else:
        draw_map(
            snap, metric, low, high, map_slot,
        )


# ---------- 趨勢 ----------
with tab_trend:
    column1, column2 = st.columns(2)

    with column1:
        st.subheader("測站 AQI 與污染物趨勢")
        top3 = snap.nlargest(3, "aqi")["station"].tolist()

        picks = st.multiselect(
            "選擇測站（可多選）",
            sorted(filtered["station"].unique()),
            default=top3,
        )

        metric1 = st.selectbox(
            "指標",
            metrics,
            format_func=LABELS.get,
            key="trend_metric",
        )
        if picks:
            line = filtered[
                filtered["station"].isin(picks)
            ].pivot_table(
                index="publishtime",
                columns="station",
                values=metric1,
                aggfunc="mean",
            )
            st.line_chart(line)
        else:
            st.info("請至少選一個測站。")

    with column2:
        st.subheader("各區域污染指標變化")
        grouping = st.radio(
            "分組方式",
            ["縣市", "區域"],
            horizontal=True,
        )

        group_column = "county" if grouping == "縣市" else "region"

        metric2 = st.selectbox(
            "指標",
            metrics,
            index=metrics.index("pm25") if "pm25" in metrics else 0,
            format_func=LABELS.get,
            key="area_metric",
        )
        area_stack_options = {
            "分層重疊（layered）": False,
            "中心堆疊（center）": "center",
            "標準化 100%（normalize）": "normalize",
        }
        area_stack_label = st.selectbox(
            "堆疊樣式",
            list(area_stack_options),
            key="area_stack",
        )

        area_data = filtered.pivot_table(
            index="publishtime",
            columns=group_column,
            values=metric2,
            aggfunc="mean",
        )

        st.area_chart(area_data, stack=area_stack_options[area_stack_label])


# ---------- 比較 ----------
with tab_cmp:
    column1, column2 = st.columns(2)

    with column1:
        st.subheader("空氣品質區域排名")

        level = st.radio(
            "比較單位",
            ["縣市", "區域", "測站"],
            horizontal=True,
        )

        group_column = {
            "縣市": "county",
            "區域": "region",
            "測站": "station",
        }[level]

        bar_content = st.selectbox(
            "圖表內容",
            ["平均 AQI 排名", "空氣品質狀態測站數"],
        )
        bar_orientation = st.radio(
            "長條方向",
            ["直向", "橫向"],
            horizontal=True,
        )
        bar_stack_options = {
            "並排": False,
            "累加堆疊": True,
            "標準化 100%": "normalize",
            "中心堆疊": "center",
            "重疊": "layered",
        }
        bar_stack_label = st.selectbox(
            "系列呈現方式",
            list(bar_stack_options),
            disabled=bar_content != "空氣品質狀態測站數",
        )
        if bar_content == "平均 AQI 排名":
            order = st.radio(
                "排序",
                ["高 → 低", "低 → 高"],
                horizontal=True,
            )
            rank = snap.groupby(group_column)[metric].mean().sort_values(
                ascending=(order == "低 → 高")
            )
            count = st.slider(
                "顯示前幾名",
                1,
                max(1, len(rank)),
                min(10, len(rank)),
                key="aqi_rank_count",
            )
            st.bar_chart(
                rank.head(count),
                horizontal=(bar_orientation == "橫向"),
            )
        else:
            status_matrix = snap.groupby(
                [group_column, "status"]
            ).size().unstack(fill_value=0).reindex(columns=status_opts, fill_value=0)
            status_matrix = status_matrix.loc[
                status_matrix.sum(axis=1).sort_values(ascending=False).index
            ]
            count = st.slider(
                "顯示前幾名",
                1,
                max(1, len(status_matrix)),
                min(10, len(status_matrix)),
                key="status_bar_count",
            )
            status_matrix = status_matrix.head(count)
            st.bar_chart(
                status_matrix,
                horizontal=(bar_orientation == "橫向"),
                stack=bar_stack_options[bar_stack_label],
            )

    with column2:
        st.subheader("污染物與空氣品質的關聯")

        options = list(metrics)

        x_metric = st.selectbox(
            "X 軸",
            options,
            index=options.index("wind_speed") if "wind_speed" in options else 0,
            format_func=LABELS.get,
        )

        y_metric = st.selectbox(
            "Y 軸",
            options,
            index=options.index("pm25") if "pm25" in options else 0,
            format_func=LABELS.get,
            key="y_axis",
        )

        category_column = st.selectbox(
            "測站分類",
            ["status", "region"],
            format_func={
                "region": "區域",
                "status": "空品狀態",
            }.get,
        )
        points = snap.dropna(subset=[x_metric, y_metric]).copy()
        category_label = {"status": "空品狀態", "region": "區域"}[category_column]
        points[category_label] = points[category_column].fillna("未分類").astype(str)
        size_options = [m for m in options if m not in {x_metric, y_metric}] or options
        size_metric = st.selectbox(
            "點大小依據指標",
            size_options,
            format_func=LABELS.get,
        )
        points["_scatter_size"] = pd.to_numeric(
            points[size_metric],
            errors="coerce",
        ).fillna(0).clip(lower=0)
        x_label = LABELS[x_metric]
        y_label = LABELS[y_metric]
        size_label = f"點大小：{LABELS[size_metric]}"
        points[x_label] = points[x_metric]
        points[y_label] = points[y_metric]
        points[size_label] = points["_scatter_size"]

        if len(points) > 2 and x_metric != y_metric:
            correlation = points[[x_metric, y_metric]].corr().iloc[0, 1]
            st.caption(
                f"相關係數 r = {correlation:.2f}（{len(points)} 個測站）"
            )

        st.scatter_chart(
            points,
            x=x_label,
            y=y_label,
            color=category_label,
            size=size_label,
        )

    column3, column4 = st.columns(2)

    with column3:
        st.subheader("各空氣品質等級測站數")
        status_counts = snap["status"].value_counts().reindex(status_opts).dropna()
        st.bar_chart(status_counts)

    with column4:
        st.subheader("目前主要污染物")
        pollutants = (
            snap["pollutant"]
            .replace(r"^\s*$", pd.NA, regex=True)
            .dropna()
            .value_counts()
        )

        if pollutants.empty:
            st.info("這個時間點沒有測站標示主要污染物（代表空氣品質良好）。")
        else:
            st.bar_chart(pollutants)


# ---------- 單一測站 ----------
with tab_station:
    names = sorted(base["station"].unique())
    default_index = names.index(worst["station"]) if worst["station"] in names else 0

    station = st.selectbox(
        "選擇測站",
        names,
        index=default_index,
    )

    one = base[base["station"] == station].sort_values("publishtime")
    current_station = one[one["publishtime"] == t_sel]

    if current_station.empty:
        st.warning("這個測站在所選時間沒有資料。")
    else:
        row = current_station.iloc[0]
        col1, col2, col3, col4 = st.columns(4)

        col1.metric(
            "AQI",
            "—" if pd.isna(row["aqi"]) else f"{row['aqi']:.0f}",
        )
        col2.metric(
            "狀態",
            row["status"] if pd.notna(row["status"]) else "—",
        )
        col3.metric(
            "主要污染物",
            row["pollutant"] if pd.notna(row.get("pollutant")) else "無",
        )
        col4.metric(
            "PM2.5",
            "—" if pd.isna(row.get("pm25")) else f"{row['pm25']:.0f}",
        )

        st.info(advice(row["aqi"]))

    show_metrics = st.multiselect(
        "要畫的指標",
        metrics,
        default=[m for m in ["aqi", "pm25"] if m in metrics],
        format_func=LABELS.get,
    )

    if show_metrics:
        chart_data = one.set_index("publishtime")[show_metrics].rename(
            columns=LABELS
        )
        st.line_chart(chart_data)


# ---------- 資料 ----------
with tab_data:
    st.caption("呈現目前時間與篩選條件下的測站觀測資料，可排序或下載 CSV。")

    columns = [
        column
        for column in [
            "publishtime", "region", "county", "sitename", "aqi",
            "status", "pollutant", "pm25", "pm10", "o3", "no2",
            "wind_speed", "latitude", "longitude",
        ]
        if column in snap.columns
    ]

    table_labels = {
        "publishtime": "發布時間",
        "region": "區域",
        "county": "縣市",
        "sitename": "測站",
        "aqi": "AQI",
        "status": "空品狀態",
        "pollutant": "主要污染物",
        "pm25": "PM2.5",
        "pm10": "PM10",
        "o3": "臭氧 O₃",
        "no2": "二氧化氮 NO₂",
        "wind_speed": "風速",
        "latitude": "緯度",
        "longitude": "經度",
    }
    st.dataframe(
        snap[columns].sort_values("aqi", ascending=False).rename(columns=table_labels),
        hide_index=True,
    )

    download1, download2 = st.columns(2)

    download1.download_button(
        "⬇ 下載目前時間點（CSV）",
        snap[columns].to_csv(index=False).encode("utf-8-sig"),
        file_name=f"aqi_{t_sel:%Y%m%d_%H%M}.csv",
        mime="text/csv",
    )

    download2.download_button(
        "⬇ 下載全部篩選結果（CSV）",
        filtered.to_csv(index=False).encode("utf-8-sig"),
        file_name="aqi_filtered.csv",
        mime="text/csv",
    )


# ---------- 進階圖表：Altair 熱圖 ----------
with tab_advanced:
    st.subheader("各測站 AQI 時序熱圖")
    st.caption(
        "以熱圖同時比較多個測站在不同時間的 AQI。顏色依 AQI 分級；"
        "將滑鼠移到色塊上可查看縣市、測站、時間與數值。"
    )

    heatmap_data = filtered.dropna(subset=["aqi", "publishtime"]).copy()
    if heatmap_data.empty:
        st.info("目前篩選條件下沒有可繪製的 AQI 時序資料。")
    else:
        station_avg = (
            heatmap_data.groupby("station")["aqi"].mean().sort_values(ascending=False)
        )
        max_stations = min(50, len(station_avg))
        station_count = st.slider(
            "顯示測站數（依平均 AQI 由高至低）",
            min_value=1,
            max_value=max_stations,
            value=min(20, max_stations),
            key="heatmap_station_count",
        )
        chosen_stations = station_avg.head(station_count).index.tolist()
        heatmap_data = heatmap_data[heatmap_data["station"].isin(chosen_stations)]
        heatmap_data["時間"] = heatmap_data["publishtime"].dt.strftime("%m/%d %H:%M")
        heatmap_data["測站"] = heatmap_data["station"]
        heatmap_data["縣市"] = heatmap_data["county"]
        heatmap_data["AQI"] = heatmap_data["aqi"]

        station_order = [station for station in chosen_stations]
        time_order = [
            value.strftime("%m/%d %H:%M")
            for value in sorted(heatmap_data["publishtime"].unique())
        ]
        heatmap = (
            alt.Chart(heatmap_data)
            .mark_rect(stroke="white", strokeWidth=0.4)
            .encode(
                x=alt.X("時間:O", sort=time_order, title="發布時間", axis=alt.Axis(labelAngle=-45)),
                y=alt.Y(
                    "測站:N",
                    sort=station_order,
                    title="測站",
                    axis=alt.Axis(labelLimit=260),
                ),
                color=alt.Color(
                    "AQI:Q",
                    title="AQI",
                    scale=alt.Scale(
                        domain=[0, 50, 100, 150, 200, 300],
                        range=["#00b050", "#ffd400", "#ff7e00", "#e60000", "#8f3f97", "#7e0023"],
                        clamp=True,
                    ),
                ),
                tooltip=[
                    alt.Tooltip("縣市:N"),
                    alt.Tooltip("測站:N"),
                    alt.Tooltip("時間:O"),
                    alt.Tooltip("AQI:Q", format=".0f"),
                ],
            )
            .properties(height=max(360, 24 * station_count))
            .interactive()
        )
        st.altair_chart(
            heatmap,
            width="stretch",
            theme="streamlit",
            alt="各空氣品質測站在不同發布時間的 AQI 熱圖",
        )
