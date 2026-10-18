# 城市交通路径导航系统（数据结构实践 · 选题 A）

> 课程实践项目：把城市地铁/公交线路抽象成图（邻接表），提供三种搜索口径并用 Tkinter 可视化。
> 三种口径：**最快路径（Dijkstra，时间/距离双权重）** · **最少换乘（0-1 BFS）** · **启发式（A\*，欧几里得可采纳 h）**。

## 当前状态

| 模块 | 负责人 | 状态 |
| --- | --- | --- |
| 数据与图模型（`core/graph.py`、`data/`） | P1 | 原型可用（双格式加载 + 校验），待补 4 组数据集与真实数据接入 |
| 算法引擎（`core/solver.py`、`tests/`、`tools/bench.py`） | P2 | ✅ **已交付**：契约 1.1.0 · 18 条单测 · 基准 CSV · 3 张流程图 · 报告第 3 章草稿 |
| 界面与交互（`ui/window.py`、`main.py`） | P3 | 原型可用（下拉/点选、状态机、结果面板），待补异常空态与对比面板 |
| 渲染与工程化（`ui/canvas.py`、`selfcheck.py`、打包） | P4 | 原型可用（绘制/高亮/动画/自检矩阵），待四份任务书对应项 |

> 四人任务书见 `docs/任务书/`；技术方案见 `docs/技术方案.md`。

## 快速开始

```powershell
# 1) 自检（退出码 0 = 全过）
python main.py --check

# 2) 单测（P2：18 条）
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
ui/canvas.py            画布渲染（P4 目标；当前绘制逻辑在 window.py）
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
2. `python -m unittest discover -s tests -t .` 全过；
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

- 运行时零第三方依赖（仅标准库 + Tkinter）；`tools/make_figs.py` 用 Pillow 画插图，属开发期工具。
- 原始任务书 PDF 未入库（课程材料，仅本地保留）。