# Wall Sundial Planner (墙面日晷刻线 API)

本地离线服务：给定地点、时区/夏令时规则、墙面方位与倾角、面板轮廓、晷针参数
和日期范围，按固定太阳算法计算晷针阴影与墙面的交点，生成小时线、季节日期线、
SVG 刻线图，并标出太阳在墙后、阴影平行于墙面、阴影落出面板、**周边建筑/檐口/
常绿树冠遮挡**及夏令时空跳等不可读时段。支持候选针长/基点/标签布局搜索、质量
检查、SQLite 版本化存储。

**不调用任何在线天文或地图服务。**

## 算法

* 太阳位置：内置 NOAA Solar Calculations（截断精度版），1900–2100 年误差
  约 0.3–0.5°（`app/solar.py`）。
* 坐标：本地 ENU（东、北、上）；墙面外法向由方位角 `azimuth`（自北顺时针）
  与倾角 `inclination`（0=竖直、90=水平朝天、负值=后仰）构造；晷针针尖沿
  `base + length * dir_unit + normal_offset * n_unit`，阴影与墙平面解析求交
  （`app/geometry.py`）。
* 遮挡轮廓（`app/obstacles.py`）：墙面版本可保存若干**命名**遮挡天际线（建筑、
  檐口、常绿树冠），每条由按太阳方位角递增的 `(azimuth_deg, altitude_deg)`
  控制点描述。开放轮廓（`wrap=false`）只在首末方位之间有效；闭合轮廓
  （`wrap=true`）首末控制点之间的封口段跨越 0°（北）方位，插值在 360/0 接缝
  处环绕。每个采样时刻对有效方位做**分段线性插值**得到遮挡高度，太阳高度角
  **不高于**该高度即判遮挡；多条轮廓同时命中时取天际线最高者（余量 = 遮挡
  高度 − 太阳高度）。
* 时刻：UTC 朴素时间 + 自带 DST 规则引擎（无规则/固定日/第 N 个星期），
  不依赖系统 tzdata（`app/timezone_engine.py`）。
* 小时线：每天每个钟点**反解精确时刻**（真太阳时经均时差反解，民用时按时区
  换算，春令时空跳钟点标记为 `dst_gap`），按日期连接；季线在指定日期整天
  细采样。
* 结果完全确定：同一输入哈希重复生成，JSON/SVG 逐字节一致。

## 安装（全新环境可复现）

需要 Python 3.11+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt        # 仅运行
# 或运行测试：
.venv/bin/pip install -r requirements-dev.txt
```

`requirements.txt` 固定三个直接依赖版本，`requirements-lock.txt` 固定全部
传递依赖版本。

## 启动

```bash
SUNDIAL_DB=/path/to/sundial.db python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 交互文档: http://127.0.0.1:8000/docs
```

不设置 `SUNDIAL_DB` 时使用仓库根目录下 `sundial.db`（SQLite 自动建表）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/dial/generate` | 无状态生成刻线、检查报告、SVG、输入哈希、遮挡报告 |
| POST | `/dial/analyze` | 同上（强调检查报告） |
| POST | `/dial/search` | 基点网格 × 候选针长 × 标签布局搜索排序 |
| POST | `/walls` | 创建墙面模型（版本 1） |
| GET | `/walls` / `/walls/{id}` | 列表 / 当前版本详情 |
| PUT | `/walls/{id}` | 改几何：哈希变化才另存新版本，否则不新增 |
| GET | `/walls/{id}/versions` | 全部不可变版本 |
| POST | `/walls/{id}/versions/{vid}/generate` | 按版本生成（带结果缓存，同哈希一致） |
| POST | `/walls/{id}/versions/{vid}/search` | 按版本搜索 |
| POST | `/walls/{id}/versions/{vid}/schemes` | 保存选定方案 |
| GET | `/walls/{id}/versions/{vid}/schemes` / `/schemes/{id}` | 方案查询 |
| POST | `/walls/{id}/versions/{vid}/inverse` | 实测阴影坐标反查时间（引用版本与方案，只读） |

### 生成请求示例

```json
{
  "wall": {
    "name": "Berlin south wall",
    "latitude": 52.52, "longitude": 13.40,
    "standard_offset_minutes": 60,
    "dst": {"mode": "nth_weekday",
            "start_month": 3, "start_weekday": "sun", "start_week": 5,
            "start_at_local": "02:00",
            "end_month": 10, "end_weekday": "sun", "end_week": 5,
            "end_at_local": "03:00", "dst_offset_minutes": 60},
    "azimuth": 180, "inclination": 0,
    "panel": [{"x":-1,"y":0},{"x":1,"y":0},{"x":1,"y":1.2},{"x":-1,"y":1.2}],
    "obstacles": [
      {"name": "evergreen hedge", "wrap": false,
       "points": [{"azimuth_deg": 120, "altitude_deg": 12},
                  {"azimuth_deg": 180, "altitude_deg": 18},
                  {"azimuth_deg": 240, "altitude_deg": 12}]},
      {"name": "rooftop ring", "wrap": true,
       "points": [{"azimuth_deg": 60, "altitude_deg": 8},
                  {"azimuth_deg": 180, "altitude_deg": 5},
                  {"azimuth_deg": 300, "altitude_deg": 8}]}
    ]
  },
  "date_range": {"start": "2026-01-01", "end": "2026-12-31"},
  "gnomon": {"base": {"x": 0.0, "y": 0.4},
             "direction": [0, -1, 0], "length": 0.25,
             "normal_offset": 0.0},
  "options": {"time_mode": "solar", "hours": [6,7,8,9,10,11,12,13,14,15,16,17,18],
              "sample_minutes": 20}
}
```

### 搜索请求要点

`search.time_mode` 取 `solar`/`civil`（civil 按民用钟点排列并标注 DST
空跳）；`candidate_lengths`、`min_spacing`、`margin`、`base_grid_step`、
`label_offsets` 及 `full_top`（决定多少个候选带完整 result）均参与
`input_hash`，任一改动哈希即变。

排序按：**扣除遮挡后的实际可读覆盖**（阴影落入面板的样本 / 墙面受照且未被
遮挡轮廓隐藏的样本）→ 最小刻线间距 → 面板占用；无任何可读时间的组合沉底。
每个候选额外给出 `obstacle_losses`：各命名轮廓在搜索采样网格上损失的样本数
与分钟数（与晷针无关，同墙所有候选一致）。检查项还包括刻线断裂
（`broken_lines`）、小时线间距过小（`spacing_issues`）、标签包围盒重叠
（`label_overlaps`）、边距与 DST 事件。

### 反查请求要点（`/versions/{vid}/inverse`）

请求体引用**已保存方案**（`scheme_id`，晷针随之冻结）与路径中的不可变墙面版本，
提交按时间先后排列的面板坐标 `observations`、观测日期 `date` **或** `date_range`
（二选一）、坐标容差 `tolerance`（米）以及相邻观测的时间间隔
`interval_min_minutes`/`interval_max_minutes`（两个及以上观测时必填，成对出现且
min ≤ max）。引用校验：墙面/版本不存在返回 404，方案不存在返回 404，方案属于
其他版本返回 422。

求解分两阶段（`app/inverse.py`）：先按 `options.coarse_step_minutes`（默认 10
分钟）在 UTC 网格上粗扫，定位每段连续可读轨迹进入容差圆盘的区间；再在连续阴影
轨迹上求根——二分求 `|P(t) − q| = tolerance` 的入/出根（轨迹段端点处改求状态
边界根），黄金分割细化得到最近接近时刻。所有不可读规则与正向生成完全一致
（`engine._classify`）：夏令时春跳、太阳在墙后、阴影平行、越出面板、遮挡轮廓
命中的时刻都从轨迹中剔除。

每个观测返回**全部**候选：UTC、民用时（`local_clock`，D/S 后缀）、真太阳时
（`solar_time_min`）、坐标残差 `residual_m`、对应轨迹段 `segment`（序号/日期/
起止）及容差窗口 `window_start/end_utc`；落在秋令时重叠小时的候选带
`dst_overlap: true`（同一民用读数对应两个 UTC）。单点落在自交轨迹附近（如不同
日期同一阴影位置）时多解全部保留。多点请求按点序与间隔筛选一致时间链
（`chains`，按总残差排序）；无法匹配时 `failure` 定位首个观测并列出排除原因
（最近接近距离与时刻、范围内各状态样本统计、每个首观测候选无法延伸的环节）。

响应带 `precision`（粗扫步长、求根精度、容差）、`input_hash`（含观测顺序）、
`geometry_hash` 与来源版本（`wall_id`/`version`/`version_id`/`scheme_id`）。
求解是纯函数：同一请求结果固定，方案与墙面版本不会被改写。

### 不可读时段状态

每条采样带 `status`：`ok` / `below_horizon` / `sun_behind_wall` /
`shadow_parallel` / `shadow_outside_panel` / `blocked_by_obstacle` /
`dst_gap`，并在 `note` 给出计算依据（如 `s·n=…`、墙平面交点坐标、命中轮廓
名称、该方位天际线高度与余量、EoT 反解残差）。被遮挡样本仍保留原始太阳方位/
高度与墙平面交点（`shadow` 为 `null`，但 `sample_basis` 可追溯）；其
`obstacle` 字段给出阻挡物名称、方位、轮廓高度与高度余量。线级 `gaps` 只统计
地平线上的失效（夜间不算刻线断裂），遮挡缺口带阻挡物名称；全局
`invalid_intervals` 按采样步长连续合并（相邻不同阻挡物不合并），不会跨夜拼接；
顶层 `obstacle_report` 汇总每条轮廓的连续遮挡时段与总时长。刻线段与 SVG 在
遮挡时段断开。

> 墙面没有任何 `obstacles` 时，结果载荷不出现 `obstacle` / `blocked_count` /
> `obstacle_report` / `obstacle_losses` 等新字段，几何哈希与旧请求完全一致；
> 只有提交了遮挡轮廓，相关数据才进入哈希与缓存。

## 测试

```bash
python3 -m pytest tests/ -q
```

## 版本与哈希

* `geometry_hash`：由地点、时区/DST、墙面方位/倾角/面板轮廓，以及（若存在）
  全部命名遮挡轮廓的名称、`wrap` 与控制点决定（不含墙名称）。修改这些参数
  再次 `PUT` 会生成新版本；几何不变则不新增版本。无遮挡的墙面不写入
  `obstacles` 键，哈希与引入遮挡功能之前保持一致。
* `input_hash`：在几何哈希之上再包含晷针、日期范围、生成选项与标签偏移
  （遮挡随几何哈希间接进入）；`/versions/{id}/generate` 按此哈希缓存，同版本
  同输入返回一致结果。
* 搜索请求使用独立的搜索哈希（含全部搜索参数与遮挡几何）。
* 遮挡轮廓随 `wall_versions.geometry_json` 冻结：旧版本永远缺少 `obstacles`
  键，按旧版本生成得到的就是旧天际线下的结果。
