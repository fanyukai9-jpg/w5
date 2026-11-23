"""臺灣空氣品質監測
資料：環境部 AQI 歷史測站資料。可切換本機 CSV 快照或即時 API；
API 失敗時會自動退回 CSV。
執行：streamlit run app.py
"""

import json
import time
from math import asin, cos, log, pi, radians, sin, sqrt, tan
from pathlib import Path
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

location_component = st.components.v2.component(
    name="air_quality_nearby_locator",
    html='<button id="locate">📍 定位並顯示附近測站</button>',
    css="""
    button {
        border: 1px solid var(--st-primary-color);
        border-radius: 0.5rem;
        padding: 0.45rem 0.8rem;
        color: var(--st-primary-color);
        background: var(--st-background-color);
        font: inherit;
        cursor: pointer;
    }
    button:disabled { opacity: 0.65; cursor: wait; }
    """,
    js="""
    export default function({ parentElement, setStateValue, data }) {
        const button = parentElement.querySelector("#locate");
        if (data?.located) button.textContent = "📍 已定位｜重新定位附近測站";
        button.onclick = () => {
            if (!navigator.geolocation) {
                setStateValue("error", {message: "此瀏覽器不支援定位功能。"});
                return;
            }
            button.disabled = true;
            button.textContent = "正在取得位置…";
            navigator.geolocation.getCurrentPosition(
                ({coords}) => {
                    setStateValue("location", {
                        latitude: coords.latitude,
                        longitude: coords.longitude,
                        accuracy: coords.accuracy,
                        requestedAt: Date.now()
                    });
                },
                (error) => {
                    setStateValue("error", {message: error.message});
                    button.disabled = false;
                    button.textContent = "📍 重新定位附近測站";
                },
                {enableHighAccuracy: false, timeout: 10000, maximumAge: 300000}
            );
        };
    }
    """,
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

# 臺灣本島縣市相鄰關係；離島只關聯自身。
COUNTY_NEIGHBORS = {
    "基隆市": ["新北市"],
    "臺北市": ["新北市"],
    "新北市": ["基隆市", "臺北市", "桃園市", "宜蘭縣"],
    "桃園市": ["新北市", "新竹縣"],
    "新竹市": ["新竹縣"],
    "新竹縣": ["桃園市", "新竹市", "苗栗縣"],
    "苗栗縣": ["新竹縣", "臺中市"],
    "臺中市": ["苗栗縣", "彰化縣", "南投縣", "花蓮縣"],
    "彰化縣": ["臺中市", "南投縣", "雲林縣"],
    "南投縣": ["臺中市", "彰化縣", "雲林縣", "嘉義縣", "高雄市", "花蓮縣"],
    "雲林縣": ["彰化縣", "南投縣", "嘉義縣"],
    "嘉義市": ["嘉義縣"],
    "嘉義縣": ["雲林縣", "南投縣", "嘉義市", "臺南市", "高雄市"],
    "臺南市": ["嘉義縣", "高雄市"],
    "高雄市": ["臺南市", "嘉義縣", "南投縣", "臺東縣", "屏東縣"],
    "屏東縣": ["高雄市", "臺東縣"],
    "宜蘭縣": ["新北市", "花蓮縣"],
    "花蓮縣": ["宜蘭縣", "臺中市", "南投縣", "臺東縣"],
    "臺東縣": ["花蓮縣", "高雄市", "屏東縣"],
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
        records = payload
    else:
        records = None
    if records is None and isinstance(payload, dict):
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
    center: tuple[float, float] = (23.7, 121.0),
    map_mode: str = "3D AQI 柱狀圖",
    highlighted_counties: list[str] | None = None,
    focused_county: str | None = None,
    selectable: bool = True,
):
    """繪製測站地圖。"""
    data = data.dropna(subset=["latitude", "longitude"]).copy()

    colors = [rgb_for(metric, value, low, high) for value in data[metric]]

    alpha = 32 if focused_county else 210
    data["rgba"] = [
        [*color, 255 if focused_county and county == focused_county else alpha]
        for color, county in zip(colors, data["county"])
    ]
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
    data["elevation"] = (
        pd.to_numeric(data[metric], errors="coerce")
        .clip(lower=0)
        .fillna(0)
        .astype(float)
    )
    if focused_county:
        data.loc[data["county"] == focused_county, "elevation"] *= 1.4
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

    if map_mode == "測站平面圖":
        layer = pdk.Layer(
            "ScatterplotLayer",
            data=data[
                [
                    "sitename", "county", "status", "latitude", "longitude",
                    "rgba", "val", "radius_m",
                ]
            ],
            get_position="[longitude, latitude]",
            get_fill_color="rgba",
            get_radius="radius_m",
            radius_min_pixels=5,
            radius_max_pixels=16,
            pickable=True,
            id="aqi-stations",
        )
    else:
        layer = pdk.Layer(
            "ColumnLayer",
            data=data[
                [
                    "sitename", "county", "status", "latitude", "longitude",
                    "rgba", "val", "elevation",
                ]
            ],
            get_position="[longitude, latitude]",
            get_elevation="elevation",
            elevation_scale=70,
            get_fill_color="rgba",
            get_radius=2200,
            radius_min_pixels=4,
            radius_max_pixels=14,
            extruded=True,
            material=True,
            pickable=True,
            id="aqi-stations",
        )

    boundary_features = load_county_boundaries()
    highlighted = set(highlighted_counties or [])
    boundary_layers = []
    if focused_county:
        dim_features = [
            feature for feature in boundary_features
            if feature.get("properties", {}).get("COUNTYNAME") != focused_county
        ]
        if dim_features:
            boundary_layers.append(
                pdk.Layer(
                    "GeoJsonLayer",
                    data={"type": "FeatureCollection", "features": dim_features},
                    id="dim-unselected-counties",
                    stroked=False,
                    filled=True,
                    get_fill_color=[18, 18, 22, 175],
                    pickable=False,
                )
            )
    if boundary_features:
        boundary_layers.append(
            pdk.Layer(
                "PolygonLayer",
                data=county_polygon_data(),
                id="county-boundaries",
                stroked=False,
                filled=True,
                get_polygon="polygon",
                get_fill_color=[0, 0, 0, 1],
                pickable=True,
                auto_highlight=True,
            )
        )
        boundary_layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                data={"type": "FeatureCollection", "features": boundary_features},
                id="county-outline",
                stroked=True,
                filled=False,
                get_line_color=[170, 180, 188, 155],
                line_width_min_pixels=0.8,
                pickable=False,
            )
        )
    highlighted_features = [
        feature for feature in boundary_features
        if feature.get("properties", {}).get("COUNTYNAME") in highlighted
    ]
    if highlighted_features:
        boundary_layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                data={"type": "FeatureCollection", "features": highlighted_features},
                id="nearby-county-outline",
                stroked=True,
                filled=False,
                get_line_color=[255, 255, 255, 255],
                line_width_min_pixels=3,
                pickable=False,
            )
        )
    if focused_county:
        focused_features = [
            feature for feature in boundary_features
            if feature.get("properties", {}).get("COUNTYNAME") == focused_county
        ]
        if focused_features:
            focused_polygons = [
                polygon for polygon in county_polygon_data()
                if polygon["county"] == focused_county
            ]
            boundary_layers.append(
                pdk.Layer(
                    "PolygonLayer",
                    data=focused_polygons,
                    id="focused-county-toggle",
                    stroked=False,
                    filled=True,
                    get_polygon="polygon",
                    get_fill_color=[0, 0, 0, 1],
                    pickable=True,
                    auto_highlight=True,
                )
            )
            boundary_layers.append(
                pdk.Layer(
                    "GeoJsonLayer",
                    data={"type": "FeatureCollection", "features": focused_features},
                    id="focused-county-outline",
                    stroked=True,
                    filled=False,
                    get_line_color=[255, 255, 255, 255],
                    line_width_min_pixels=4,
                    pickable=False,
                )
            )

    deck = pdk.Deck(
        layers=[*boundary_layers, layer],
        views=[pdk.View(type="MapView", controller=True)],
        initial_view_state=pdk.ViewState(
            latitude=center[0],
            longitude=center[1],
            zoom=zoom,
            pitch=58 if map_mode == "3D AQI 柱狀圖" else 0,
            bearing=0,
        ),
        tooltip={
            "html": (
                f"<b>{{county}} {{sitename}}</b><br/>"
                f"{LABELS[metric]}：{{val}}<br/>{{status}}"
            )
        },
    )

    if selectable:
        chart_key = (
            "air_quality_county_map_"
            f"{st.session_state.get('map_selection_epoch', 0)}_"
            f"{'flat' if map_mode == '測站平面圖' else '3d'}_"
            f"{focused_county or 'all'}"
        )
        event = slot.pydeck_chart(
            deck,
            on_select="rerun",
            selection_mode="single-object",
            key=chart_key,
        )
        selection = getattr(event, "selection", {}) or {}
        selected_by_layer = selection.get("objects", {})

        def selected_county_name(selected_objects):
            if not selected_objects:
                return None
            selected_object = selected_objects[0]
            properties = selected_object.get("properties", selected_object)
            return (
                properties.get("COUNTYNAME")
                or properties.get("county")
                or selected_object.get("county")
                or selected_object.get("COUNTYNAME")
            )

        next_focus = st.session_state.get("map_focus_county")
        boundary_objects = selected_by_layer.get("county-boundaries")
        boundary_county = selected_county_name(boundary_objects)
        if focused_county and boundary_county and boundary_county != focused_county:
            next_focus = boundary_county
        elif focused_county and selected_by_layer.get("focused-county-toggle"):
            next_focus = None
        elif "county-boundaries" in selected_by_layer:
            next_focus = boundary_county

        if next_focus != st.session_state.get("map_focus_county"):
            st.session_state["map_focus_county"] = next_focus
            if next_focus is None:
                st.session_state["map_selection_epoch"] = (
                    st.session_state.get("map_selection_epoch", 0) + 1
                )
            st.rerun()
    else:
        slot.pydeck_chart(deck, key="animated_air_quality_map")


@st.cache_data
def load_county_boundaries():
    """載入縣界並略過遠離臺灣周邊的外海多邊形。"""
    path = Path(__file__).with_name("tw_county_boundaries.geojson")
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as boundary_file:
        features = json.load(boundary_file).get("features", [])

    west, east, south, north = 117.0, 123.5, 20.0, 27.5

    def polygon_is_near_taiwan(polygon):
        points = []

        def collect(value):
            if (
                isinstance(value, (list, tuple))
                and len(value) >= 2
                and isinstance(value[0], (int, float))
                and isinstance(value[1], (int, float))
            ):
                points.append(value)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    collect(child)

        collect(polygon)
        return bool(points) and all(
            west <= point[0] <= east and south <= point[1] <= north
            for point in points
        )

    for feature in features:
        geometry = feature.get("geometry", {})
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates", [])
        if geometry_type == "MultiPolygon":
            geometry["coordinates"] = [
                polygon for polygon in coordinates if polygon_is_near_taiwan(polygon)
            ]
        elif geometry_type == "Polygon" and not polygon_is_near_taiwan(coordinates):
            geometry["coordinates"] = []
    return [
        feature for feature in features
        if feature.get("geometry", {}).get("coordinates")
    ]


@st.cache_data
def county_polygon_data():
    """Flatten GeoJSON polygons so PyDeck can report county names on clicks."""
    polygons = []
    for feature in load_county_boundaries():
        geometry = feature.get("geometry", {})
        coordinates = geometry.get("coordinates", [])
        if geometry.get("type") == "Polygon":
            county_polygons = [coordinates]
        elif geometry.get("type") == "MultiPolygon":
            county_polygons = coordinates
        else:
            continue
        county = feature.get("properties", {}).get("COUNTYNAME", "")
        for polygon in county_polygons:
            polygons.append(
                {
                    "county": county,
                    "polygon": polygon,
                    "sitename": "縣市界",
                    "val": "—",
                    "status": "點一下聚焦縣市",
                }
            )
    return polygons


def county_view(counties: list[str]):
    """依縣界範圍估算適合的地圖中心與縮放倍率。"""
    wanted = set(counties)
    points = []

    def walk_coordinates(value):
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            points.append((float(value[0]), float(value[1])))
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk_coordinates(child)

    for feature in load_county_boundaries():
        if feature.get("properties", {}).get("COUNTYNAME") in wanted:
            walk_coordinates(feature.get("geometry", {}).get("coordinates", []))
    if not points:
        return (23.7, 121.0), 6.4

    west = min(point[0] for point in points)
    east = max(point[0] for point in points)
    south = min(point[1] for point in points)
    north = max(point[1] for point in points)
    center = ((south + north) / 2, (west + east) / 2)

    def mercator_y(latitude):
        latitude = max(-85.0, min(85.0, latitude))
        latitude_radians = radians(latitude)
        return log(tan(pi / 4 + latitude_radians / 2))

    longitude_fraction = max((east - west) / 360, 1e-5)
    latitude_fraction = max(
        abs(mercator_y(north) - mercator_y(south)) / (2 * pi),
        1e-5,
    )
    zoom_x = log(620 / (512 * longitude_fraction), 2)
    zoom_y = log(390 / (512 * latitude_fraction), 2)
    return center, max(5.0, min(11.0, min(zoom_x, zoom_y) - 0.5))


def nearest_stations(data: pd.DataFrame, latitude: float, longitude: float):
    """計算目前資料中每個測站與使用者位置的直線距離。"""
    data = data.dropna(subset=["latitude", "longitude"]).copy()
    lat1, lon1 = radians(latitude), radians(longitude)

    def distance_km(row):
        lat2, lon2 = radians(row["latitude"]), radians(row["longitude"])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        return 6371 * 2 * asin(sqrt(a))

    data["距離（公里）"] = data.apply(distance_km, axis=1)
    return data.sort_values("距離（公里）")


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

    refresh_mode = st.sidebar.selectbox(
        "資料更新",
        ["手動更新", "整點更新"],
        index=1,
        key="refresh_mode",
    )

    if refresh_mode == "整點更新" and api_key:
        now = datetime.now()
        interval_to_hour = (
            (60 - now.minute) * 60 - now.second
        ) * 1000 - now.microsecond // 1000
        st_autorefresh(
            interval=max(interval_to_hour, 1000),
            key="aqi_hourly_refresh",
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

# 定位完成後，記錄最近測站所在縣市及其相鄰縣市作為地圖的預設視野。
locator_state = st.session_state.get("nearby_location_component")
component_location = (
    locator_state.get("location")
    if isinstance(locator_state, dict)
    else getattr(locator_state, "location", None)
)
if component_location:
    st.session_state["nearby_location"] = component_location
location = st.session_state.get("nearby_location")
if location:
    location_token = (
        round(float(location["latitude"]), 4),
        round(float(location["longitude"]), 4),
        location.get("requestedAt"),
    )
    if location_token != st.session_state.get("applied_location_token"):
        latest_sites = raw[raw["publishtime"] == times[-1]]
        closest = nearest_stations(
            latest_sites,
            float(location["latitude"]),
            float(location["longitude"]),
        )
        if not closest.empty:
            anchor_county = str(closest.iloc[0]["county"])
            nearby_counties = [
                anchor_county,
                *COUNTY_NEIGHBORS.get(anchor_county, []),
            ]
            available_counties = set(raw["county"].dropna().unique())
            nearby_counties = sorted(
                set(nearby_counties) & available_counties
            )
            st.session_state["nearby_anchor_county"] = anchor_county
            st.session_state["nearby_counties"] = nearby_counties
            st.session_state["applied_location_token"] = location_token
            st.session_state["map_focus_county"] = None
            st.session_state["map_selection_epoch"] = (
                st.session_state.get("map_selection_epoch", 0) + 1
            )


# ---------- 側邊欄：篩選條件 ----------
regions_all = [
    region
    for region in [*REGIONS, "其他"]
    if region in raw["region"].unique()
]

if "region_filter" not in st.session_state:
    st.session_state["region_filter"] = regions_all
region_pick = st.sidebar.multiselect(
    "區域",
    regions_all,
    key="region_filter",
)

county_opts = sorted(
    raw[raw["region"].isin(region_pick)]["county"].unique()
)
previous_regions = st.session_state.get("_last_region_filter")
if previous_regions is not None and set(previous_regions) != set(region_pick):
    st.session_state["county_filter"] = county_opts
st.session_state["_last_region_filter"] = list(region_pick)
if "county_filter" not in st.session_state:
    st.session_state["county_filter"] = county_opts

county_pick = st.sidebar.multiselect(
    "縣市",
    county_opts,
    key="county_filter",
)

metric = st.sidebar.selectbox(
    "地圖與排名指標",
    metrics,
    format_func=LABELS.get,
)

if source == "即時 API" and follow_latest:
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
location_scope_note = ""
if st.session_state.get("nearby_anchor_county"):
    location_scope_note = (
        f"｜定位周邊：{st.session_state['nearby_anchor_county']}及相鄰縣市"
    )
st.caption(
    "即時掌握各地空氣品質，追蹤污染變化，並比較區域與測站。\n\n"
    f"資料來源：{source_note}｜"
    f"資料範圍：{times[0]:%m/%d %H:%M}–{times[-1]:%m/%d %H:%M}｜"
    f"目前檢視：{t_sel:%m/%d %H:%M}｜"
    f"符合條件的測站：{len(snap)} / "
    f"{base[base['publishtime'] == t_sel].shape[0]}{location_scope_note}"
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


tab_map, tab_trend, tab_cmp, tab_station, tab_station_time, tab_data = st.tabs(
    ["即時概況", "時間趨勢", "區域比較", "測站分析", "測站 AQI 時序", "資料明細"]
)


# ---------- 地圖 ----------
with tab_map:
    st.caption(
        f"全台測站都會顯示；定位附近縣市以粗線標出。"
        f"顏色代表{LABELS[metric]}級距，平面圖點大小或 3D 柱高代表數值高低。"
        "定位後地圖會放大到附近區域；點任一縣市可聚焦，再點同一縣市恢復。"
        "聚焦時其他縣市與測站調暗，聚焦測站會加亮並加粗標示。"
        "可拖曳平移、滾輪縮放、按住右鍵旋轉。"
        "縣市界線來源：內政部國土測繪中心。"
    )

    location_result = location_component(
        data={"located": bool(st.session_state.get("nearby_location"))},
        default={"location": None, "error": None},
        on_location_change=lambda: None,
        on_error_change=lambda: None,
        key="nearby_location_component",
    )
    if location_result.location:
        st.session_state["nearby_location"] = location_result.location
        st.session_state.pop("nearby_location_error", None)
    if location_result.error:
        st.session_state["nearby_location_error"] = location_result.error.get(
            "message", "無法取得定位。"
        )
    if st.session_state.get("nearby_location_error"):
        st.warning(
            "無法取得位置。請確認瀏覽器已允許定位，或繼續使用全台測站地圖。"
        )

    map_mode = st.radio(
        "地圖樣式",
        ["測站平面圖", "3D AQI 柱狀圖"],
        horizontal=True,
        index=1,
        key="map_mode",
    )

    map_data = raw[
        (raw["publishtime"] == t_sel)
        & raw["aqi"].ge(aqi_min)
        & raw["status"].isin(status_pick)
    ]
    map_center = (23.7, 121.0)
    map_zoom = 6.4
    location = st.session_state.get("nearby_location")
    highlighted_counties = st.session_state.get("nearby_counties", [])
    focused_county = st.session_state.get("map_focus_county")
    if highlighted_counties:
        map_center, map_zoom = county_view(highlighted_counties)
    if focused_county:
        map_center, map_zoom = county_view([focused_county])
    if location:
        lat, lon = location["latitude"], location["longitude"]
        nearby = nearest_stations(map_data, lat, lon)
        if nearby.empty:
            st.info("目前條件下沒有可定位的測站，請調整 AQI 或狀態篩選。")
        else:
            st.subheader("📍 你附近的空品測站")
            st.caption(
                "依距離列出最近 5 站；附近縣市以粗線標示，其餘縣市和測站仍完整顯示。"
            )
            nearby_display = nearby.head(5).copy()
            nearby_display["距離（公里）"] = nearby_display["距離（公里）"].round(1)
            st.dataframe(
                nearby_display[
                    ["county", "sitename", "距離（公里）", "aqi", "status"]
                ].rename(
                    columns={
                        "county": "縣市",
                        "sitename": "測站",
                        "aqi": "AQI",
                        "status": "狀態",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )

    if focused_county:
        st.info(f"目前聚焦：{focused_county}｜再點一次該縣市可恢復附近區域視野。")

    play = st.button("▶ 播放這段時間的地圖動畫")
    caption_slot = st.empty()
    map_slot = st.empty()

    if play:
        for timestamp in times:
            frame = raw[
                (raw["publishtime"] == timestamp)
                & raw["aqi"].ge(aqi_min)
                & raw["status"].isin(status_pick)
            ]
            caption_slot.markdown(
                f"**{timestamp:%m/%d %H:%M}**　"
                f"{LABELS[metric]}　測站 {len(frame)}"
            )
            draw_map(
                frame, metric, low, high, map_slot,
                zoom=map_zoom,
                center=map_center,
                map_mode=map_mode,
                highlighted_counties=highlighted_counties,
                focused_county=focused_county,
                selectable=False,
            )
            time.sleep(0.8)
    else:
        draw_map(
            map_data, metric, low, high, map_slot,
            zoom=map_zoom,
            center=map_center,
            map_mode=map_mode,
            highlighted_counties=highlighted_counties,
            focused_county=focused_county,
        )


# ---------- 趨勢 ----------
with tab_trend:
    column1, column2 = st.columns(2)

    with column1:
        st.subheader("測站 AQI 與污染物趨勢")
        top3 = snap.nlargest(3, "aqi")["station"].tolist()
        location = st.session_state.get("nearby_location")
        nearby_counties = set(st.session_state.get("nearby_counties", []))
        if location and nearby_counties:
            nearby_now = snap[snap["county"].isin(nearby_counties)]
            preferred = nearest_stations(
                nearby_now,
                location["latitude"],
                location["longitude"],
            )["station"].drop_duplicates().head(3).tolist()
            top3 = (preferred + [name for name in top3 if name not in preferred])[:3]

        picks = st.multiselect(
            "選擇測站（可多選）",
            sorted(
                filtered["station"].unique(),
                key=lambda name: (
                    filtered.loc[filtered["station"] == name, "county"].iloc[0]
                    not in nearby_counties,
                    name,
                ),
            ),
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
        st.subheader("多縣市污染指標變化")
        st.caption("多個縣市共用時間軸並重疊顯示，呈現方式依課堂面積圖範例。")

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

        area_options = sorted(filtered[group_column].dropna().unique())
        group_means = filtered.groupby(group_column)[metric2].mean().dropna()
        default_series_order = group_means.sort_values(ascending=False).index.tolist()
        nearby_counties = set(st.session_state.get("nearby_counties", []))
        if grouping == "縣市":
            nearby_groups = nearby_counties
        else:
            nearby_groups = set(
                raw.loc[raw["county"].isin(nearby_counties), "region"].dropna()
            )
        default_series = [
            group for group in default_series_order if group in nearby_groups
        ][:2]
        default_series += [
            group for group in default_series_order if group not in default_series
        ][: 2 - len(default_series)]
        area_options = sorted(
            area_options,
            key=lambda group: (group not in nearby_groups, group),
        )
        selected_series = st.multiselect(
            f"放在同一張圖的{grouping}（最多 5 個）",
            area_options,
            default=default_series,
            max_selections=5,
            key=f"area_series_{group_column}",
        )

        area_data = filtered[filtered[group_column].isin(selected_series)].pivot_table(
            index="publishtime",
            columns=group_column,
            values=metric2,
            aggfunc="mean",
        )
        if area_data.empty:
            st.info(f"目前篩選條件下沒有可繪製的 {LABELS[metric2]} 資料。")
        else:
            st.area_chart(area_data, stack=False)


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
    location = st.session_state.get("nearby_location")
    if location:
        nearest_current = nearest_stations(
            snap[snap["station"].isin(names)],
            location["latitude"],
            location["longitude"],
        )
        if not nearest_current.empty:
            default_index = names.index(nearest_current.iloc[0]["station"])

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
with tab_station_time:
    st.subheader("縣市測站 AQI 時序分布")
    st.caption(
        "選擇縣市查看該縣市各測站隨時間的 AQI 變化；移到色塊上可查看測站、時間與 AQI。"
    )

    heatmap_data = filtered.dropna(subset=["aqi", "publishtime"]).copy()
    if heatmap_data.empty:
        st.info("目前篩選條件下沒有可繪製的 AQI 時序資料。")
    else:
        county_options = sorted(heatmap_data["county"].dropna().unique())
        if not county_options:
            st.info("目前篩選條件下沒有可選的縣市。")
        else:
            nearby_anchor = st.session_state.get("nearby_anchor_county")
            nearby_counties = st.session_state.get("nearby_counties", [])
            default_counties = []
            if nearby_anchor in county_options:
                default_counties.append(nearby_anchor)
            default_counties.extend(
                county for county in nearby_counties
                if county in county_options and county not in default_counties
            )
            if not default_counties:
                county_avg = heatmap_data.groupby("county")["aqi"].mean()
                default_counties = [str(county_avg.idxmax())]
            default_counties = default_counties[:5]

            selected_before = st.session_state.get("heatmap_counties")
            if "heatmap_counties" not in st.session_state:
                st.session_state["heatmap_counties"] = default_counties
            elif isinstance(selected_before, list):
                st.session_state["heatmap_counties"] = [
                    county for county in selected_before if county in county_options
                ][:5]
            else:
                st.session_state["heatmap_counties"] = default_counties

            selected_counties = st.multiselect(
                "選擇縣市（可多選）",
                county_options,
                key="heatmap_counties",
                max_selections=5,
                placeholder="輸入縣市名稱快速搜尋",
                help="可同時比較最多 5 個縣市；附近縣市會優先預選。",
            )
            if not selected_counties:
                st.info("請選擇至少一個縣市以顯示時序分布。")
            else:
                heatmap_data = heatmap_data[
                    heatmap_data["county"].isin(selected_counties)
                ].copy()
                station_order = []
                for county in selected_counties:
                    county_stations = (
                        heatmap_data.loc[heatmap_data["county"] == county]
                        .groupby("station")["aqi"]
                        .mean()
                        .sort_values(ascending=False)
                        .index.tolist()
                    )
                    station_order.extend(county_stations)

                st.caption(
                    f"已選 {len(selected_counties)} 個縣市、"
                    f"{len(station_order)} 個測站。"
                )
                heatmap_data["時間"] = heatmap_data["publishtime"].dt.strftime("%m/%d %H:%M")
                heatmap_data["測站"] = heatmap_data["station"]
                heatmap_data["縣市"] = heatmap_data["county"]
                heatmap_data["AQI"] = heatmap_data["aqi"]

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
                    .properties(height=max(360, 24 * len(station_order)))
                    .interactive()
                )
                st.altair_chart(
                    heatmap,
                    width="stretch",
                    theme="streamlit",
                    alt="各空氣品質測站 AQI 時序分布圖",
                )
