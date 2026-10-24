# 城市交通路径导航系统（数据结构实践 · 选题 A）

> 课程实践项目：把城市地铁/公交线路抽象成图（邻接表），提供三种搜索口径并用 Tkinter 可视化。
> 三种口径：**最快路径（Dijkstra，时间/距离双权重）** · **最少换乘（0-1 BFS）** · **启发式（A\*，欧几里得可采纳 h）**。

## 当前状态

| 模块 | 负责人 | 状态 |
| --- | --- | --- |
| 数据与图模型（`core/graph.py`、`data/`） | P1 | 原型可用（双格式加载 + 校验），待补 4 组数据集与真实数据接入 |
| 算法引擎（`core/solver.py`、`tests/`、`tools/bench.py`） | P2 | ✅ **已交付**：契约 1.2.0 · 23 条单测 · 基准 CSV · 3 张流程图 · 报告第 3 章草稿 |
| 界面与交互（`ui/window.py`、`main.py`） | P3 | 原型可用（下拉/点选、状态机、结果面板），待补异常空态与对比面板 |
| 渲染与工程化（`ui/canvas.py`、`selfcheck.py`、打包） | P4 | 原型可用（绘制/高亮/动画/自检矩阵），待四份任务书对应项 |

> 四人任务书见 `docs/任务书/`；技术方案见 `docs/技术方案.md`。

## 快速开始

```powershell
# 1) 自检（退出码 0 = 全过）
python main.py --check

# 2) 单测（P2：23 条）
python -m unittest discover -s tests -t .

# 3) 界面
python main.py

# 4) 命令行对比三算法
python main.py --cli 大学城 机场 --all-pairs

# 5) 基准（有 CV 噪声闸门）
python tools/bench.py --out tools/bench.csv --repeats 7 --pairs 3 --check-cv 15
```

## 目录结构

```text
main.py                 入口：GUI / --check / --cli / --smoke
selfcheck.py            自检矩阵（10 组断言，退出码即闸门）
core/graph.py           图模型 + JSON/TXT 加载 + 校验
core/solver.py          算法引擎：Dijkstra / A* / 0-1 BFS 最少换乘
ui/window.py            Tkinter 主窗（控件 / 状态机 / 结果面板）
ui/canvas.py            画布渲染（P4 目标，**尚未创建**；当前绘制逻辑在 window.py）
data/demo_city.json     示例城市（与 .txt 等价）
tests/                  P2 单测矩阵（support.py 含独立 oracle）
tools/bench.py          基准与对比实验（CSV）
tools/make_figs.py      报告插图生成（开发期用 Pillow）
docs/                   技术方案 / 四份任务书 / 基准文档 / 契约变更 / 插图
report/                 实验报告草稿（第 3 章 + 第 2 章供稿）
```

## 算法口径（报告与答辩可直接引用）

- **可采纳性**：距离权重 `h = 欧氏直线距离`；时间权重 `h = 欧氏 / v_max × 60`。
- **最少换乘必须用 `(站点, 线路)` 状态**：对站点直接 BFS 只会得到"最少站数"。
- **扩展计数**：出堆且首次访问记 1；BFS 的单位是**状态**不是站点（对比表须加注）。
- 复杂度：Dijkstra `O((V+E)logV)` · 0-1 BFS `O(V·L+E)` · A\* 最坏 `O(E logV)`。

## 质量门槛

1. `python main.py --check` 全绿；
2. `python -m unittest discover -s tests -t .`（23 条）全过；
3. `python tools/bench.py --check-cv 15` 通过（CV 中位 ≤ 15%）；
4. UI 改动附截图（站名不重叠、控件不被裁切）。

## 文档

| 文档 | 说明 |
| --- | --- |
| `docs/技术方案.md` | 架构、数据模型、算法、界面、接口契约、验收矩阵 |
| `docs/任务书/` | 四份个人任务书 + 共享总览（P1–P4 各 14/15/14/16 点） |
| `docs/bench.md` | 复杂度推导 + 实测数据 + 计时口径与坑 |
| `docs/契约变更.md` | 版本 1.1.0 变更 + 待 P1 决策的数据精度问题 |
| `docs/P2-交付清单.md` | P2 交付物绝对路径清单 |
| `docs/交接口径-P2给P1P3P4.md` | P2 → P1/P3/P4 的接口与口径交接（含待 P1 决策项） |
| `docs/交接/` | 按人拆分的口径单：`P1-收到的口径.md` · `P3-收到的口径.md` · `P4-收到的口径.md` |
| `docs/评审-P4代码-2026-10-18.md` | 对 P4 仓库的外部代码评审（实测闸门 + 14 项发现 + 整改顺序） |
| `docs/评审-自有代码-2026-10-18.md` | 自有代码评审（1 高已修 + 6 中 + 10 低，含探针证据与整改顺序） |
| `report/` | 报告草稿（第 3 章算法 + 第 2 章供稿） |

## 备注

- 开发期依赖：Pillow（仅 `tools/make_figs.py` 画插图与 PNG 导出）→ `requirements-dev.txt`。

- 运行时零第三方依赖（仅标准库 + Tkinter）；`tools/make_figs.py` 用 Pillow 画插图，属开发期工具。
- 原始任务书 PDF 未入库（课程材料，仅本地保留）。

## 界面（P3 整合，2026-10-18）

主界面已切换为 P3 的窗口实现（`ui/window.py` + `ui/widgets.py`），画布用 P4 的
`CanvasView`（vendored 到 `ui/p4_canvas.py`，P2 写了 `ui/canvas.py` 适配层）。

```powershell
python main.py                       # 打开界面（P3 主窗）
python main.py --smoke _smoke/ui.ps   # 无人工冒烟：建窗→搜索→导出画布
```

P3 的交付说明、11 张截图与第 4 章草稿在 `docs/P3/`；适配细节见 `docs/P3/整合说明.md`。

## 169/164 站显示层示意布局（2026-10-24 修复）

> 用户验收口径：「164 个节点时每个路线不重叠」。

### 现象与根因

`data/real_metro.json`（169 站）的站坐标由 `tools/import_metro.py` 按「每条线一条水平带、
x=站序×1.5、y=线序×8、换乘站取首次出现坐标」生成。换乘站被合并到其它线路坐标后，同一线路
相邻站之间出现 8~64 km 的「跳跃直线」——188 条边中 45 条 >10 km，实测 460 处非相邻线段交叉，
运行时线路在画布中央糊成一束、互相压盖（基线证据：`docs/布局修复证据/README.md`）。

### 方案（只动显示层，不动数据 / 算法 / 交互）

新增 `ui/display_layout.py`（纯标准库，仅显示用）：

- `station_positions(graph, line_sequences)`：优先读 `graph.meta['display_layout']`（预生成布局注入口）；
  `needs_schematic_layout()` 判定为「生成器线性近似坐标」时用 `schematic_layout()` 重算示意坐标
  （BFS 全对距离 → 最远点采样 pivot → PivotMDS → 力导向细化）；否则返回数据坐标（10 站 demo 不触发）。
  并行线段的判定与分离使用「与书写方向无关的投影 + 最近点对方向 + 自适应阻尼后处理」，避免反向近平行漏判。
- `ui/canvas.py::_CanvasGraph` 用显示坐标构造 `_DisplayStation` 代理（不改 P1 的 `core.Station`，
  A* 启发式仍用数据坐标，算法数值不变）。
- `ui/p4_canvas.py` 两处：`_segment_metrics` 改用数据层坐标算 km/min（界面分段标注与算法一致）；
  `_draw_lines` 对「多条线路共用同一对相邻站」的并行走廊向两侧偏移画小弧线
  （2/7 号线 广州南站—石壁）。

### 验证命令

```powershell
$py = "python"
& $py main.py --check                                    # 自检：ALL PASS
& $py -m unittest discover -s tests -t .                 # 单测：68 条，OK (skipped=3 为 bench 数据预存在跳过)
& $py main.py --smoke _smoke/metro.ps --data data/real_metro.json
& $py main.py --smoke _smoke/stress164.ps --data data/stress_164.json
& $py tools/layout_report.py --out _reports              # 布局几何独立验证（169/164/demo）
& $py tools/export_canvas.py --data data/real_metro.json --start S001 --end S169 `
    --png out.png --no-animation                         # 截图（P4-06 导出脚本）
```

### 验收结果（主验收目标：169 站）

> 用户最终口径：「不用太复杂，只需要 169 节点即可」——**169 站为最终验收目标**；
> 164 站与随机线网属附加鲁棒性验证，不阻塞。

- **169 站（`data/real_metro.json`，主验收）**：并行重叠 10→0（lead 统一口径，正常交叉不计）、
  跨线近重合(3px) 22→0、交叉 434→2（仅正常交叉，构图正常可读）、线段穿站 26→0、
  站名/标注两两重叠 0、布局耗时 ≈0.53 s。
- 附加验证（不阻塞）：164 站同口径并行重叠 9→0、近重合 36→0、交叉 428→2、穿站 33→0（≈0.54 s）；
  随机线网鲁棒性压测 38/38 通过（见 `_reports/stress_report.md`）。
- demo（10 站）：不触发布局重算，画布**逐像素不变**（PostScript 除 CreationDate 外逐字节相同）。
- 算法数值不变：同一批点对 Result 的 `distance_km / time_min / transfers / expanded` 与修复前完全一致；
  `core/`、`ui/window.py` 与只读基准 `_orig/path-nav` 逐字节相同。
- 证据截图：`docs/布局修复证据/`；完整回归报告：`docs/布局修复证据/README.md`。

### 已知边界

- 示意布局只表示**拓扑关系**，不表示真实地理方位/比例；正常线路交叉保留（地铁示意图惯例）。
- 共用同一对相邻站的并行线路在数据坐标层必然重合，画布渲染层用 ±4 px 小弧线视觉分开；
  极端密集数据下站名仍按 LOD 抽稀（策略与修复前一致）。
