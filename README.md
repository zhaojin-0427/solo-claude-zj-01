# Wall Sundial Planner (墙面日晷刻线 API)

本地离线服务：给定地点、时区/夏令时规则、墙面方位与倾角、面板轮廓、晷针参数
和日期范围，按固定太阳算法计算晷针阴影与墙面的交点，生成小时线、季节日期线、
SVG 刻线图，并标出太阳在墙后、阴影平行于墙面、阴影落出面板及夏令时空跳等
不可读时段。支持候选针长/基点/标签布局搜索、质量检查、SQLite 版本化存储。

**不调用任何在线天文或地图服务。**

## 算法

* 太阳位置：内置 NOAA Solar Calculations（截断精度版），1900–2100 年误差
  约 0.3–0.5°（`app/solar.py`）。
* 坐标：本地 ENU（东、北、上）；墙面外法向由方位角 `azimuth`（自北顺时针）
  与倾角 `inclination`（0=竖直、90=水平朝天、负值=后仰）构造；晷针针尖沿
  `base + length * dir_unit + normal_offset * n_unit`，阴影与墙平面解析求交
  （`app/geometry.py`）。
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
| POST | `/dial/generate` | 无状态生成刻线、检查报告、SVG、输入哈希 |
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
    "panel": [{"x":-1,"y":0},{"x":1,"y":0},{"x":1,"y":1.2},{"x":-1,"y":1.2}]
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

排序按：可读时段覆盖（阴影落入面板的样本 / 墙面实际受照样本）→ 最小刻线
间距 → 面板占用；无任何可读时间的组合沉底。检查项还包括刻线断裂
（`broken_lines`）、小时线间距过小（`spacing_issues`）、标签包围盒重叠
（`label_overlaps`）、边距与 DST 事件。

### 不可读时段状态

每条采样带 `status`：`ok` / `below_horizon` / `sun_behind_wall` /
`shadow_parallel` / `shadow_outside_panel` / `dst_gap`，并在 `note` 给出
计算依据（如 `s·n=…`、墙平面交点坐标、EoT 反解残差）。线级 `gaps` 只统计
地平线上的失效（夜间不算刻线断裂）；全局 `invalid_intervals` 按采样步长
连续合并，不会跨夜拼接。

## 测试

```bash
python3 -m pytest tests/ -q
```

## 版本与哈希

* `geometry_hash`：仅由地点、时区/DST、墙面方位/倾角/面板轮廓决定（不含名称）。
  修改这些参数再次 `PUT` 会生成新版本；几何不变则不新增版本。
* `input_hash`：在几何哈希之上再包含晷针、日期范围、生成选项与标签偏移；
  `/versions/{id}/generate` 按此哈希缓存，同版本同输入返回一致结果。
* 搜索请求使用独立的搜索哈希（含全部搜索参数）。
