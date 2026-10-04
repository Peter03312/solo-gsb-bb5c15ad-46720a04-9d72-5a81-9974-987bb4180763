# 高校课程学期规划服务

基于 **Python + FastAPI + SQLite** 的课程学期规划服务：依据课程目录（学分、春秋开课学期、
先修条件、同修课程），为学生生成满足全部约束、**期限内最早完成**的完整排课方案；
目录支持版本化修订，方案绑定目录版本，可回看、可确认。

教务员还可按**培养方案编号登记要求版本**（必修课程 + 按优先级排列的选修组，绑定一个既有
课程目录版本），并以要求版本与学生已修课程清单生成**可回看的达成审核**；审核为快照，
后续目录或要求修订不改变历史审核结果。

## 功能与规则

- **课程目录**：每门课记录学分、开课季节（`SPRING`/`FALL`，可两者）、先修条件与同修课程。
  - 先修条件按“组”声明：**每组任选一门，各组均须满足**；先修课必须安排在**更早学期**。
  - 同修课程（coreq）尚未修过时，必须与该课**同学期**安排。
  - 可随版本声明**互不重叠的课程互认组**（见下文「课程互认组」）：组内课程可替代
    满足目标课程及先修组选项；未声明时行为与之前一致。
- **规划输入**：已修课程、目标课程（非空）、起始学期（如 `2026-SPRING`）、
  每学期学分上限、规划学期数；可选**锁定安排**（课程+学期）与**不可排课学期**。
  学分与两个上限必须为正，非法输入一律拒绝（422/400）。
- **锁定安排（可选）**：`locked` 为 `{course, semester}` 列表，锁定课程视为**本次必修**
  （即使不在目标课程中），并必须安排在指定学期。锁定课程不得是已修课、不得重复锁定，
  学期须在规划期限内；锁定课程仍须满足开课季节、先修组、同修同学期与学分上限。
- **不可排课学期（可选）**：`unavailable_semesters` 列出的学期不安排任何课程
  （期限外的学期自动忽略）。
- **规划输出**：
  - 已修课程不重复排入；方案只包含目标课程、锁定课程及满足条件所需的课程。
  - 返回期限内**最早完成**的完整方案；完成学期相同者，按“各学期课程代码序列”
    （每学期内代码升序、再按学期先后组成序列）的**字典序**取最小者。
  - 课程缺失、条件冲突（先修循环、同修课无共同开课学期、锁定与约束冲突等）
    或期限内无解时，返回具体原因，**不返回部分课表**。
- **目录版本**：每次目录修订生成新版本；方案创建时绑定当前版本，可随时回看；
  确认方案时若目录已修订，则拒绝（409）且不写入。
- **执行中途滚动重排（草案）**：已确认方案（**普通规划或培养方案达成课表**）执行过程中，
  如实际完成情况与计划不同或目录已修订，可凭**原方案编号、已结束学期数、截至该时点的
  累计已修课程**重新排课：
  - 已结束学期数必须 **大于 0 且小于原规划期限**；累计已修**不得撤销**原方案创建时声明的已修
    （必须是其超集，可包含计划外提前修完的课程）。
  - 从原起始学期顺延至下一学期起，沿用**原每学期学分上限和剩余学期数**，按**当前目录**
    求最早完成的完整未来课表；已修课程不重复排入。普通规划沿用原目标课程；培养方案
    达成课表沿用**原要求版本与绑定目录**，拟修课及选修归属可重新选择，要求归属与计入学分
    仍按绑定目录计算，并随草案返回预计归属（仅凭累计已修即达成时未来课表为空）。
  - 返回新课表以及与**原方案剩余学期**（跳过已结束学期）的课程差异（逐学期新增/移除 + 汇总），
    并作为**待确认草案**（`status="draft"`）关联原方案保存；原方案及其历史目录保持不变。
  - 草案沿用既有确认约定：`POST /plans/{draft_id}/confirm`，目录在草案生成后再修订则 409 拒绝；
    培养方案草案确认时同样仅替换来源方案尚未结束学期的占位。
  - 原方案不存在或未确认、目标缺失、条件冲突、期限内无解、撤销原已修或学期数越界时，
    返回具体原因且**不保存**；目录在计算至落库之间被修订时，拒绝旧版本结果（409，不保存）。
- **席位上限与余量**：可按**课程代码 + 学期**设置**非负席位上限**（未设置表示不限额）：
  - 设置时校验课程须在**当前目录版本**中、学期格式合法；上限**低于该 (课程,学期) 已占用席位**
    时拒绝（409，原上限不变）；`cap=0` 表示该学期已不可再占。
  - 查询返回该 (课程,学期) 的 `cap`（上限，`null` 为不限额）、`occupied`（已占用）、
    `remaining`（余量，`null` 为不限额）。
  - **规划与滚动重排均基于当时余量**：规划引擎把席位作为硬约束，在仍满足先修、同修、互认、
    锁定、不可排课学期等既有规则的前提下，于有剩余名额的学期中求**最早完成 + 字典序**课表；
    待确认方案与重排草案**不占位**。容量不足导致无可行课表时返回 `NO_SOLUTION` 并说明
    已满的课程/学期（上限与已占用），**不保存部分方案**。
  - **确认时原子复核**：在单个数据库事务（立即加写锁）内复核**目录版本**与课表中
    **每门课每个学期**的席位并一次性占位；余量变化导致不足时返回 `SEAT_CONFLICT`（409），
    整笔事务回滚——拒绝确认、**原方案与原占位保留、绝不超卖**。
  - **重排确认的占位替换**：确认重排草案时，仅将**来源方案尚未结束学期**
    （晚于 `elapsed_semesters` 的学期）的占位替换为新课表；**已结束学期占位保留**。
    同一来源方案**至多一个生效重排结果**：已有生效重排后再确认其他草案返回 `DRAFT_EXPIRED`
    （409）；目录在草案生成后修订同样拒绝（过期）；对已生效草案**重复确认幂等**、不重复占位。
  - **互认按实际修读课程占位**：方案结果里上替代课（如 CS402）就只占替代课名额，
    不会计入原课（CS401）的席位。
- **培养方案要求版本（本轮新增）**：教务员按**培养方案编号**登记要求版本，绑定一个
  **既有课程目录版本**；同一编号内版本号自 1 递增，要求一经登记即不可修改。
  - 要求内容为**必修课程**列表与按**优先级**（声明顺序）排列的**选修组**，每组给出
    候选课程与**最低学分门槛**（正整数）；同一门课可以出现在多个必修/选修要求中。
  - 未知绑定目录（404）、候选课程不在绑定目录、重复必修项、非正学分门槛一律拒绝（422/404）
    且**不留记录**。
- **达成审核（本轮新增）**：以要求版本 + 实际已修课程清单生成，审核结果落库、可回看。
  - 互认课程（取自绑定目录版本声明的互认组）可满足对应的必修项或选修候选；
    但**一门实际已修课只归属一项**，同一互认组的多门已修课**至多计入一门**
    （跨必修与所有选修组的全局约束）。
  - 优化目标依次为：① 最大化满足的**必修项数**；② 按选修组**优先级顺序**最大化各组
    计入学分（每组计入不超过该组门槛，超出部分不占用，可让给后续选修组）；
    ③ 仍相同时取**归属课程代码序列**（必修按声明顺序、选修按组顺序，组内代码升序）
    的字典序最小者。
  - 返回各项归属（直接计入/互认计入及实际课程学分）、**未满足的必修项**、各组
    **选修学分缺口**、未归属课程与**总体是否达成**。
  - 审核只依赖绑定目录版本的学分与互认组，是**快照**：后续目录或要求修订不改变历史审核；
    未知已修课程拒绝（422）且不留记录。
  - 现有规划、滚动重排、确认与席位行为保持兼容，不受影响。
- **批次联合排课（本轮新增）**：教务处一次提交**多份普通课表申请**，每份带**唯一非空
  申请标识**及与单份规划完全相同的输入，批次在**同一当前目录与席位余量快照**上联合排课：
  - 实际修读课程在**同一学期**的**批次席位总需求**不得超额（不预占席位，仅联合求解）。
  - 确定性结果依次按：① **最大化获得完整课表的申请数**；② 按申请标识升序比较
    **完成学期**（未入选视为晚于任何完成学期）；③ 按申请标识升序比较入选课表
    **逐学期课程代码序列**（未入选视为最末）确定唯一解。
  - 返回各申请的完整课表（成员 `plan_id`，可按现有方案回看）或**未入选原因**
    （独立无解给出 `NO_SOLUTION` 等原因；可独立排课但联合席位不足给出 `BATCH_CAPACITY`
    与超额课程学期）。**至少一份可排**才保存为待确认批次；**无一可排**返回 422
    `NO_SOLUTION` 且**不保存批次**。
  - 结果**待确认、不预占席位**；`POST /plan-batches/{id}/confirm` 在**单事务**
    （立即加写锁）内复核**目录版本**与**整批席位**（全部入选课表需求合并计数），
    成功则**一并占位**并把成员课表置为已确认；目录版本变化或整批余量不足则**整批
    拒绝、原占位不变、绝不超卖**；重复确认**幂等**。
  - 批次成员课表**不得单独确认**（409 `BATCH_CONFIRM_REQUIRED`），必须整批确认；
    但仍可用现有 `GET /plans/{id}` 回看。**标识重复或任一申请参数非法（含未知课程、
    学期格式错误等）时整批拒绝（422/400）且不保存**。
  - 现有单份规划、确认、回看、滚动重排、审核与启动方式**完全保持兼容**。

## 本轮新增（多份普通课表申请的批次联合排课）

- 新增 **批次申请** `POST /plan-batches`：一次提交多份**普通课表申请**，每份携带
  **唯一非空申请标识**（`application_id`）与单份规划的全部输入（已修、目标、起始学期、
  学分上限、学期数，可选锁定/不可排课学期）。批次在**同一当前目录版本与当时席位余量
  快照**上联合排课。
- **批次席位总需求约束**：实际修读课程在**同一学期**的批次总需求不得超过席位余量；
  待确认批次**不预占席位**。各申请仍独立满足开课季节、先修组、同修、互认、锁定、
  不可排课学期与学分上限。
- **唯一结果的三级目标**：① 最大化获得完整课表的申请数；② 按申请标识升序比较
  **完成学期**（未入选晚于任何完成学期）；③ 按申请标识升序比较**入选课表逐学期课程
  代码序列**（每学期代码升序、再按学期先后；未入选视为最末）。
- **返回与保存**：逐申请返回完整课表（成员 `plan_id`，可 `GET /plans/{id}` 回看）或
  未入选原因（`reason_code` + 说明）。**至少一份可排**才保存待确认批次；**无一可排**
  返回 422 `NO_SOLUTION`、**不保存批次**。
- **整批确认**：`POST /plan-batches/{batch_id}/confirm` 在**单事务**（立即加写锁）内
  复核目录版本与整批席位（入选课表需求合并计数），成功**一并占位**；目录修订
  409 `CATALOG_CHANGED`、整批余量不足 409 `SEAT_CONFLICT`，均**整批拒绝、原占位不变、
  不超卖**；重复确认幂等。成员课表单独确认返回 409 `BATCH_CONFIRM_REQUIRED`。
- **整批拒绝**：标识重复/为空、申请数超限、任一申请参数非法（空目标、非正上限/学期数、
  非法学期、未知课程、重复锁定等）时整批拒绝（422/400）且**不保存**。
- 新增 `GET /plan-batches`（批次列表）与 `GET /plan-batches/{id}`（回看批次）；
  现有单份规划、确认、回看、重排、审核与席位接口、启动方式全部**保持兼容**。
  用法见下文「批次联合排课」。

## 上一轮新增（培养方案达成课表的执行中途重排）

- **同一接口支持两类来源**：`POST /plans/{id}/replan` 现可对**已确认的培养方案达成课表**
  发起执行中途重排（普通规划重排行为不变）。凭**来源方案编号、已结束学期数、累计已修
  课程**生成关联原方案的待确认草案；沿用**原要求版本、原每学期学分上限和原期限**。
- **两版目录继续分工**：未来课表按**当前目录**的开课、先修、同修与席位约束排课（目录
  修订后即按新版执行）；**要求归属及计入学分仍按要求绑定目录**计算，拟修课与选修归属
  均可重新选择。草案返回未来课表、**预计归属 `attribution`**（结构同达成课表）及与原
  剩余课表的逐学期/汇总差异；若仅凭累计已修即达成，草案的**未来课表为空**（仍保存）。
- **累计已修规则**：必须包含来源申请时声明的全部已修课（回退即 422
  `COMPLETED_REVOKED`）；已结束学期课表与占位不变，未来**不重复安排已修课**。计划外
  修读的**当前目录独有课**可作为后续课程的先修/同修基础，但不归属、不计学分；两版目录
  均无的已修课 422 `COURSE_MISSING`。
- **确认沿用既有契约**：`POST /plans/{draft_id}/confirm` 在单事务内原子复核目录与席位，
  **仅替换来源方案尚未结束学期的占位**，已结束学期占位保留；同一来源至多一个生效重排
  （再确认其他草案 409 `DRAFT_EXPIRED`），重复确认幂等；草案生成后目录再修订 409
  `CATALOG_CHANGED`。草案不预占席位。
- **拒绝条件**：来源不是已确认的培养方案达成课表（或普通规划）、已结束学期数不大于 0
  或不小于原规划学期数、已修清单回退时一律拒绝且**不写入草案**；期限内无解 422
  `NO_SOLUTION`（席位原因说明已满课程学期）。普通规划、重排与审核接口保持兼容，启动
  方式不变。用法见下文「执行中途滚动重排」。

## 更早一轮新增（培养方案达成课表，可发起执行中途重排）

- 新增 **达成课表申请**：`POST /requirement-versions/{rv_id}/plans`，学生凭要求版本、
  累计已修课程、起始学期、每学期学分上限与规划学期数，求期限内最早达成全部必修与
  选修门槛的完整课表，并返回合并已修与拟修后的预计归属。
- **要求绑定目录**决定互认与计入学分（仅两版目录均有的拟修课可计入要求；已修课须在
  绑定目录中存在），**当前目录**决定开课、先修、同修、排课学分与席位（当前目录独有
  课程只作先修等必要课，不归属、不计学分）。
- 同完成学期按各学期课程代码序列字典序决胜；归属遵守一门只归属一项、同互认组至多
  计一门。已达成返回 200 空课表（不保存）；未知要求版本/已修课、期限内无解说明原因
  且不保存部分方案。
- 拟修课不写成历史审核；课表待确认、不预占席位，确认沿用既有
  `POST /plans/{id}/confirm`（目录版本与席位原子复核）。现有规划、审核、重排、确认与
  席位接口行为保持兼容。

## 更早一轮新增（培养方案要求与达成审核）

- 新增 **要求版本登记**：`POST /programs/{培养方案编号}/requirement-versions`，绑定既有
  课程目录版本，列必修课与按优先级排列的选修组（候选课程 + 正学分门槛）；同门课可出现在
  不同要求中。未知目录、未知课程、重复必修项、非正门槛均拒绝且不留记录。
- 新增 **达成审核**：`POST /requirement-versions/{rv_id}/audits`，按"先最大化必修满足数、
  再按选修组顺序最大化各组封顶学分、再按归属课程代码序列字典序"给出唯一确定的归属；
  互认课可替代计入，但一门已修课只归属一项、同一互认组至多计入一门。返回各项归属、
  未满足必修、各组学分缺口与总体是否达成。
- 审核为**可回看快照**（`GET /audits`、`GET /audits/{id}`），后续目录或要求修订不改变
  历史审核；启动方式（Docker Compose / uvicorn、自动建表与种子目录）与既有规划、重排、
  确认、席位接口和数据结构保持兼容，详见下文「培养方案要求版本与达成审核」。

## 快速开始

### 方式一：Docker Compose（推荐，一键启动）

```bash
docker compose up --build
```

### 方式二：本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 访问地址

| 内容 | 地址 |
| --- | --- |
| 服务地址 | http://localhost:8000 |
| 交互式 API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

端口：容器内外均为 **8000**（Compose 映射 `8000:8000`，可在 `docker-compose.yml` 中修改）。

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | 容器内 `/data/app.db`；本地 `data/app.db` | SQLite 数据库文件路径 |

数据库在首次启动时自动初始化：建表，并写入种子课程目录（版本 1）。
Compose 通过命名卷 `planner-data` 持久化数据。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/catalog/versions` | 目录修订：全量提交课程集合（可选互认组），生成新版本 |
| GET | `/catalog/current` | 当前目录版本及全部课程（含互认组） |
| GET | `/catalog/versions/{id}` | 查看历史目录版本（供回看） |
| PUT | `/seat-caps/{course}/{semester}` | 按课程+学期设置非负席位上限（覆盖式；未设置即不限额） |
| GET | `/seat-caps/{course}/{semester}` | 查询单个席位：上限 `cap`、已占用 `occupied`、余量 `remaining` |
| GET | `/seat-caps` | 列出全部已设置的席位上限 |
| POST | `/plans` | 生成规划方案（绑定当前目录版本；基于当时余量；可选锁定/不可排课） |
| GET | `/plans` | 方案列表 |
| GET | `/plans/{id}` | 回看方案（含绑定的目录版本号） |
| POST | `/plans/{id}/confirm` | 确认方案/草案：事务内原子复核目录版本与每门课席位并占位；失败 409 回滚不超卖 |
| POST | `/plans/{id}/replan` | 已确认方案（普通规划或培养方案达成课表）执行中途滚动重排，生成关联草案（201，不占位） |
| POST | `/plan-batches` | 一次提交多份普通课表申请（唯一非空标识+规划输入），同目录与余量快照联合排课（201；无一可排 422 不保存） |
| GET | `/plan-batches` | 批次列表 |
| GET | `/plan-batches/{id}` | 回看批次（逐申请完整课表或未入选原因、绑定目录版本、确认状态） |
| POST | `/plan-batches/{id}/confirm` | 整批确认：单事务原子复核目录版本与整批席位并一并占位；失败 409 整批拒绝、原占位不变；重复确认幂等 |
| POST | `/programs/{program_code}/requirement-versions` | 登记培养方案要求版本（绑定既有目录版本；编号内版本号自增） |
| GET | `/programs/requirement-versions` | 列出全部培养方案的要求版本 |
| GET | `/programs/{program_code}/requirement-versions` | 列出某培养方案编号的全部要求版本 |
| GET | `/programs/requirement-versions/{rv_id}` | 回看单个要求版本（必修 + 按优先级排列的选修组） |
| POST | `/requirement-versions/{rv_id}/audits` | 以要求版本与已修清单生成达成审核（快照落库，201） |
| GET | `/audits` | 审核列表（可按 `?requirement_version_id=` 过滤） |
| GET | `/audits/{id}` | 回看历史审核（结果不随后续修订变化） |
| POST | `/requirement-versions/{rv_id}/plans` | 凭要求版本+已修+期限生成达成课表（绑定目录管互认/学分，当前目录管开课/先修/席位；201；已达成返回 200 空课表） |

### 目录课程字段

```json
{
  "code": "CS301",
  "name": "算法设计与分析",
  "credits": 3,
  "semesters": ["SPRING", "FALL"],
  "prereq_groups": [["CS201", "CS202"], ["MATH103"]],
  "coreqs": []
}
```

- `credits`：正整数；`semesters`：`SPRING`/`FALL` 至少其一。
- `prereq_groups`：每个内层数组为一组，组内任选一门，各组均须满足。
- `coreqs`：同修课程代码列表。

目录提交还可携带可选的 `equivalence_groups`（课程互认组，见下节）；
`GET /catalog/current` 与 `GET /catalog/versions/{id}` 的响应均含
`equivalence_groups` 字段（未声明互认组的历史版本返回空列表，其余字段不变）。

### 课程互认组

目录版本可声明**互不重叠的课程互认组**：组内课程可相互替代，满足**目标课程**
与**先修组选项**。规则：

- **已修同组课程视为已满足**，不重复排课（目标与先修组均适用）。
- **未修时择一实际修读**：所选课仍受自身开课季节、学分、先修及同修约束；
  替代先修须安排在被依赖课程**之前**的学期。方案列出实际修读的课程。
- **一次修读满足多处引用**：同组课程被多个目标或先修组引用时，修读一门即可。
- **互认不豁免同修与锁定**：同修仍按指定课程同学期修读（已修除外）；
  锁定安排仍要求指定课程在指定学期修读，同组课程不能顶替。
- **按目录版本判断**：规划与滚动重排各自按其使用的目录版本判断互认；
  既有目录与方案回看保持原版本语义。
- 仍按**最早完成**、同完成学期按实际修读课程代码序列**字典序**取优；
  无可行替代时说明冲突或期限内无解，不保存部分方案。

提交与校验（含不存在课程、重复归组或少于两门均拒绝 **422**，且不生成版本）：

```bash
curl -X POST http://localhost:8000/catalog/versions \
  -H 'Content-Type: application/json' -d @examples/catalog_v3_equiv.json
# -> 201 {"version_id": 2, "equivalence_groups": [["CS301","CS302"],["CS401","CS402"]], ...}
```

规划请求字段不变，互认按当前目录版本自动生效：

```bash
# 已修 CS302（与 CS301 同组）视同满足 CS301，CS401 的先修直接满足
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS302"],
  "target_courses": ["CS401"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 6
}'
# -> 201：2026-FALL 即完成（CS301 不再排入）

# 目标替代：目标 CS401，实际择修同组的 CS402（春季即可修读，方案列出实际修读课程）
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "completed_courses": ["CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS301"],
  "target_courses": ["CS401"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 6
}'
# -> 201：2026-SPRING 修读 CS402 即完成

# 互认不豁免锁定：CS401 仅秋季开课，锁定到春季时同组 CS402 不能顶替
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6,
  "locked": [{"course": "CS401", "semester": "2027-SPRING"}]
}'
# -> 422 REQUIREMENT_CONFLICT（锁定与约束冲突）
```

滚动重排同样按**当前目录版本**判断互认（已修 CS302 后重排，CS301 不再排入）：

```bash
curl -X POST http://localhost:8000/plans/<plan_id>/replan \
  -H 'Content-Type: application/json' -d '{
    "elapsed_semesters": 2,
    "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS302"]
  }'
```

### 培养方案要求版本与达成审核

#### 1. 登记要求版本（教务员）

按培养方案编号登记，`catalog_version_id` 必须指向一个**既有**课程目录版本；
必修课程与选修组中引用的课程都必须存在于该绑定版本中。选修组按数组顺序确定
**优先级**，每组的最低学分门槛必须为正整数。同一门课允许出现在不同必修项/选修组中；
同一编号再次登记自动成为 v2、v3……，旧版本保持不变。

```bash
# catalog_version_id=3 绑定含互认组（CS301/CS302、CS401/CS402）的目录版本
curl -X POST http://localhost:8000/programs/CS-BACH-2024/requirement-versions \
  -H 'Content-Type: application/json' -d @examples/requirement_cs_2024.json
```

请求体（`examples/requirement_cs_2024.json`）：

```json
{
  "program_code": "CS-BACH-2024",
  "catalog_version_id": 3,
  "note": "计算机科学与技术 2024 级培养方案",
  "required_courses": ["CS101", "CS201", "CS301", "CS401"],
  "elective_groups": [
    {"name": "专业选修", "candidates": ["CS303", "CS402"], "min_credits": 3},
    {"name": "基础拓展", "candidates": ["MATH101", "MATH102", "MATH103"], "min_credits": 6}
  ]
}
```

响应（201）：

```json
{
  "requirement_version_id": "CS-BACH-2024:v1",
  "program_code": "CS-BACH-2024",
  "version": 1,
  "catalog_version_id": 3,
  "required_courses": ["CS101", "CS201", "CS301", "CS401"],
  "elective_groups": [ ... ],
  "created_at": "..."
}
```

回看与列表：

```bash
curl http://localhost:8000/programs/CS-BACH-2024/requirement-versions     # 该编号全部版本
curl http://localhost:8000/programs/requirement-versions                  # 全部培养方案
curl http://localhost:8000/programs/requirement-versions/CS-BACH-2024:v1  # 单个要求版本
```

登记校验（均拒绝且**不留记录**）：

```bash
# 绑定目录版本不存在 -> 404 NOT_FOUND
curl -X POST .../programs/X/requirement-versions -d '{"program_code":"X","catalog_version_id":999}'
# 必修/选修候选引用绑定目录中不存在的课程 -> 422 COURSE_MISSING
# 重复必修项（含大小写重复）-> 422 INVALID_INPUT
-d '{"program_code":"X","catalog_version_id":1,"required_courses":["CS101","CS101"]}'
# 非正学分门槛 / 空候选组 -> 422 INVALID_INPUT
```

#### 2. 生成达成审核

提交实际已修课程清单（课程须在绑定目录版本中；未知课程 422 且不留记录）：

```bash
curl -X POST http://localhost:8000/requirement-versions/CS-BACH-2024:v1/audits \
  -H 'Content-Type: application/json' -d '{
    "completed_courses": ["CS101","CS201","CS302","CS402","CS303","MATH101","MATH103"]
  }'
```

响应（201，节选）——已修 **CS302 经互认满足必修 CS301、CS402 满足 CS401**；
CS402 已归属必修，专业选修只能由 CS303 计入（一门课只归属一项）；
基础拓展 MATH101(4)+MATH103(3) 计入 6 学分（封顶到门槛），总体达成：

```json
{
  "audit_id": "f62ed6f9fdc5",
  "requirement_version_id": "CS-BACH-2024:v1",
  "catalog_version_id": 3,
  "completed_courses": ["CS101", "CS201", "CS302", "CS303", "CS402", "MATH101", "MATH103"],
  "result": {
    "required": [
      {"code": "CS101", "satisfied": true,
       "attributed_course": {"code": "CS101", "credits": 3, "via": "direct"}},
      {"code": "CS301", "satisfied": true,
       "attributed_course": {"code": "CS302", "credits": 3, "via": "equivalence"}},
      {"code": "CS401", "satisfied": true,
       "attributed_course": {"code": "CS402", "credits": 3, "via": "equivalence"}}
    ],
    "elective_groups": [
      {"index": 1, "name": "专业选修", "candidates": ["CS303", "CS402"],
       "min_credits": 3, "counted_credits": 3, "credit_gap": 0, "satisfied": true,
       "attributed_courses": [
         {"code": "CS303", "credits": 3, "satisfies": "CS303", "via": "direct"}]},
      {"index": 2, "name": "基础拓展", "candidates": ["MATH101", "MATH102", "MATH103"],
       "min_credits": 6, "counted_credits": 6, "credit_gap": 0, "satisfied": true,
       "attributed_courses": [
         {"code": "MATH101", "credits": 4, "satisfies": "MATH101", "via": "direct"},
         {"code": "MATH103", "credits": 3, "satisfies": "MATH103", "via": "direct"}]}
    ],
    "unsatisfied_required": [],
    "unattributed_courses": [],
    "achieved": true
  }
}
```

未达成时同样保存审核（用于回看缺口），例如只修了 `["CS101","CS201","CS303"]`：

```json
{
  "unsatisfied_required": ["CS301", "CS401"],
  "elective_groups": [
    {"name": "专业选修", "counted_credits": 3, "credit_gap": 0, "satisfied": true},
    {"name": "基础拓展", "counted_credits": 0, "credit_gap": 6, "satisfied": false}
  ],
  "achieved": false
}
```

**归属优化规则**（决定课程归属的确定性结果）：

1. **先最大化满足的必修项数**——课程同时能满足必修与选修时让给必修；
2. 再按选修组声明顺序逐组最大化**计入学分**（`min(门槛, 实际学分之和)`）；
   高优先级组达门槛后不再占用多余课程，课程可让给后续组；
3. 以上仍相同，按归属课程代码序列字典序取最小（必修按声明顺序，选修按组顺序，
   组内代码升序；未满足的必修项/未计入课程的空组排在所有已归属课程之后）。
4. 全局约束：一门已修课只归属一项；同一互认组内多门已修课至多计入一门，
   即使它们同时出现在必修和多个选修组中。互认计入时学分按**实际已修课程**计算。

回看与过滤：

```bash
curl http://localhost:8000/audits/f62ed6f9fdc5                       # 历史审核（快照）
curl 'http://localhost:8000/audits?requirement_version_id=CS-BACH-2024:v1'  # 按要求版本过滤
```

历史审核保存的是生成时的快照（含 `catalog_version_id`）：之后目录再修订
（学分、互认组变化）或同编号登记了 v2 要求版本，`GET /audits/{id}` 的结果都保持不变。

### 培养方案达成课表（上一轮新增）

学生凭**培养方案要求版本、累计已修课程、起始学期、每学期学分上限和规划学期数**，
求期限内**最早**使全部必修项与各选修组学分门槛达成的完整课表。两版目录各司其职：

- **要求绑定目录版本**决定**互认与计入学分**：互认替代、归属、门槛学分一律以该版本为准；
  可计入要求的已修课必须存在于绑定目录中（执行中途计划外修读的当前目录独有课仅可作
  排课基础）；**仅两版目录均有的课程**拟修后才可计入要求，绑定目录独有（当前已不开）
  的课程无法拟修。
- **当前目录版本**决定未来课程的**开课学期、先修、同修、排课学分与席位余量**：
  课表按当前目录排；**当前目录独有课程只可作为先修/同修等必要课程**随闭包排入，
  不归属、不计入任何要求学分。

```bash
curl -X POST http://localhost:8000/requirement-versions/CS-BACH-2024:v1/plans \
  -H 'Content-Type: application/json' -d '{
    "completed_courses": ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
    "start_semester": "2026-SPRING",
    "max_credits_per_semester": 12,
    "num_semesters": 6
  }'
```

响应（201，节选）：课表按当前目录排出最早完成方案，并返回**合并已修与拟修后**的
预计归属（`attribution`，结构与达成审核一致）：

```json
{
  "plan_id": "a91c0f7e2b44",
  "status": "proposed",
  "plan_type": "program_plan",
  "catalog_version": 3,
  "bound_catalog_version": 3,
  "requirement_version_id": "CS-BACH-2024:v1",
  "request": {"plan_type": "program_plan", "requirement_version_id": "CS-BACH-2024:v1", ...},
  "result": {
    "completion_semester": "2026-FALL",
    "total_credits": 10,
    "semesters": [
      {"semester": "2026-SPRING", "courses": [{"code": "CS301", ...}, {"code": "CS303", ...}], "total_credits": 6},
      {"semester": "2026-FALL",   "courses": [{"code": "CS401", ...}], "total_credits": 4}
    ]
  },
  "attribution": {
    "requirement_version_id": "CS-BACH-2024:v1",
    "bound_catalog_version": 3,
    "completed_courses": ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
    "result": {
      "required": [ ... 各项归属（直接计入/互认计入，含实际课程学分）... ],
      "elective_groups": [ ... 各组计入课程、封顶学分与缺口 ... ],
      "unsatisfied_required": [],
      "unattributed_courses": [{"code": "CS101L", "credits": 1}],
      "achieved": true
    }
  }
}
```

规则与边界：

- **最早完成 + 字典序决胜**：所有能达成全部要求的候选选课集合，先经当前目录的
  先修/同修闭包（当前目录独有的必要课在此阶段加入）再排课；取完成学期最早者，
  相同者按“各学期课程代码序列”（学期内升序、再按学期先后）的字典序取最小。
- **预计归属确定且唯一**：归属按达成审核的同一优化规则计算——先最大化必修满足数，
  再按选修组优先级最大化各组封顶学分，最后按归属课程代码序列字典序；并严格遵守
  **一门合并后的实际修读课只归属一项、同一互认组至多计入一门**。
  拟修课的门槛学分按**绑定目录**学分，课表学期学分按**当前目录**学分。
- **已修课以绑定目录为准**：可计入要求的已修课须在绑定目录中；两版目录均无的已修课
  422 `COURSE_MISSING`。执行中途计划外修读的**当前目录独有课**允许出现（只作为后续
  课程的先修/同修基础，不归属、不计学分）；未知要求版本 404 `NOT_FOUND`；必修在当前
  目录中既未修过也无两版共有的替代课时 422 `REQUIREMENT_CONFLICT`。
- **已达成即空课表**：仅凭已修课程已满足全部要求时返回 **200** 空课表
  （`status="already_achieved"`），**不保存、不占位、不写审核**。
- **拟修课不写成历史审核**：归属是预计（`attribution`），随课表返回但**不写入**
  `audits`；审核接口仍只接受真实已修清单。
- **待确认且不预占席位**：课表以 `status="proposed"` 落库（可 `GET /plans/{id}` 回看），
  规划基于当时余量、但不占座；**确认沿用既有接口** `POST /plans/{plan_id}/confirm`——
  单事务原子复核**目录版本**（生成后目录再修订返回 409 `CATALOG_CHANGED`）与课表中
  每门课每学期席位（余量不足 409 `SEAT_CONFLICT`，回滚不超卖），互认按实际修读课程占位。
- **期限内无解不保存**：开课季节/先修顺序/学期学分上限或席位余量导致期限内无法达成时
  返回 422 `NO_SOLUTION`（席位原因含已满的课程学期与上限/已占用），**不保存部分方案**；
  组合过多返回 422 `TOO_COMPLEX`。
- 现有的规划、滚动重排、审核、确认与席位接口行为与数据结构保持兼容；普通规划
  响应不含 `attribution`/`plan_type` 字段。

### 调用示例

生成规划：

```bash
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "completed_courses": [],
  "target_courses": ["CS401"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 6
}'
```

响应（201，节选）：

```json
{
  "plan_id": "64adff786f4c",
  "status": "proposed",
  "catalog_version": 1,
  "result": {
    "completion_semester": "2027-FALL",
    "total_credits": 16,
    "semesters": [
      {"semester": "2026-SPRING", "courses": [{"code": "CS101", ...}, {"code": "CS101L", ...}], "total_credits": 4},
      {"semester": "2026-FALL",   "courses": [{"code": "CS201", ...}, {"code": "MATH103", ...}], "total_credits": 6},
      {"semester": "2027-SPRING", "courses": [{"code": "CS301", ...}], "total_credits": 3},
      {"semester": "2027-FALL",   "courses": [{"code": "CS401", ...}], "total_credits": 3}
    ]
  }
}
```

确认 / 修订 / 回看：

```bash
curl -X POST http://localhost:8000/plans/<plan_id>/confirm      # 确认方案
curl -X POST http://localhost:8000/catalog/versions \
  -H 'Content-Type: application/json' -d @examples/catalog_v2.json  # 目录修订 -> 新版本
curl http://localhost:8000/plans/<plan_id>                       # 回看方案（含绑定版本）
curl http://localhost:8000/catalog/versions/1                    # 回看历史目录
```

完整可运行示例脚本（含错误演示）：

```bash
bash examples/demo.sh
```

### 锁定安排与不可排课学期

`POST /plans` 接受两个可选字段（不带这两个字段的既有请求行为与返回结构不变）：

- `locked`：`{"course": "课程代码", "semester": "YYYY-SPRING|FALL"}` 列表。
  锁定课程视为本次必修（即使不在 `target_courses` 中），必须安排在指定学期；
  学期须在规划期限内，课程不得是已修课、不得重复锁定。
- `unavailable_semesters`：学期标签列表，这些学期不安排任何课程。

```bash
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "completed_courses": [],
  "target_courses": ["CS401"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 6,
  "locked": [{"course": "CS303", "semester": "2027-SPRING"}],
  "unavailable_semesters": ["2026-FALL"]
}'
```

注意锁定与其余约束共同生效：上例中 CS303 锁定在 2027-SPRING，要求其先修 CS201
排在更早学期，而 2026-FALL 不可排课，CS201 无处可排，因此该组合返回
`REQUIREMENT_CONFLICT`（锁定与约束冲突）。换成可行组合：

```bash
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 6,
  "locked": [{"course": "CS401", "semester": "2028-FALL"}],
  "unavailable_semesters": ["2026-FALL"]
}'
```

响应（201，节选）：2026-FALL 为不可排课学期不排课，CS401 固定在锁定学期
2028-FALL；方案仍绑定当前目录版本，可确认、可回看。

```json
{
  "plan_id": "e5f1a2b3c4d6",
  "status": "proposed",
  "catalog_version": 1,
  "request": {
    "target_courses": ["CS401"],
    "locked": [{"course": "CS401", "semester": "2028-FALL"}],
    "unavailable_semesters": ["2026-FALL"]
  },
  "result": {
    "completion_semester": "2028-FALL",
    "total_credits": 16,
    "semesters": [
      {"semester": "2026-SPRING", "courses": [], "total_credits": 0},
      {"semester": "2026-FALL",   "courses": [], "total_credits": 0},
      {"semester": "2027-SPRING", "courses": [{"code": "CS101", ...}, {"code": "CS101L", ...}], "total_credits": 4},
      {"semester": "2027-FALL",   "courses": [{"code": "CS201", ...}, {"code": "MATH103", ...}], "total_credits": 6},
      {"semester": "2028-SPRING", "courses": [{"code": "CS301", ...}], "total_credits": 3},
      {"semester": "2028-FALL",   "courses": [{"code": "CS401", ...}], "total_credits": 3}
    ]
  }
}
```

单独锁定一门非目标课程（视为本次必修，即使不在 `target_courses` 中）：

```bash
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"],
  "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12,
  "num_semesters": 6,
  "locked": [{"course": "CS303", "semester": "2027-SPRING"}]
}'
# -> 201：CS303 排入 2027-SPRING，完成学期 2027-FALL
```

锁定相关失败均说明原因且不保存方案，例如：

```json
{"detail": {"code": "INVALID_INPUT", "message": "课程 CS303 被重复锁定"}}
{"detail": {"code": "INVALID_INPUT", "message": "锁定课程 CS101 已是已修课程，不得再锁定排课"}}
{"detail": {"code": "INVALID_INPUT", "message": "锁定课程 CS401 的学期 2030-FALL 不在规划期限（2026-SPRING 起共 6 个学期）内"}}
{"detail": {"code": "COURSE_MISSING", "message": "课程缺失：ABC123 不在当前目录版本中"}}
{"detail": {"code": "REQUIREMENT_CONFLICT", "message": "锁定课程 CS401 在锁定学期 2027-SPRING 不开课（该课及其同修课程仅 FALL 开课），锁定与约束冲突"}}
{"detail": {"code": "REQUIREMENT_CONFLICT", "message": "锁定课程 CS401 的学期 2027-FALL 为不可排课学期，锁定与约束冲突"}}
{"detail": {"code": "NO_SOLUTION", "message": "受锁定安排、开课学期、先修顺序或每学期学分上限 12 限制，4 个学期内无法排完所需课程"}}
```

### 执行中途滚动重排

仅可对**已确认**方案发起；重排结果以**草案**形式关联保存，原方案不变。
请求只需原方案编号、已结束学期数与累计已修课程（目标、学分上限、起始学期均取自原方案，
期限取剩余学期数，目录取当前版本）：

```bash
curl -X POST http://localhost:8000/plans/<plan_id>/replan \
  -H 'Content-Type: application/json' -d '{
    "elapsed_semesters": 2,
    "completed_courses": ["CS101", "CS101L", "CS201", "MATH103", "CS301"]
  }'
```

响应（201，节选）：

```json
{
  "plan_id": "a1b2c3d4e5f6",
  "status": "draft",
  "catalog_version": 2,
  "source_plan_id": "64adff786f4c",
  "request": {
    "completed_courses": ["CS101", "CS101L", "CS201", "CS301", "MATH103"],
    "target_courses": ["CS401"],
    "start_semester": "2027-SPRING",
    "max_credits_per_semester": 12,
    "num_semesters": 4
  },
  "result": {
    "completion_semester": "2027-FALL",
    "total_credits": 7,
    "semesters": [
      {"semester": "2027-SPRING", "courses": [], "total_credits": 0},
      {"semester": "2027-FALL", "courses": [{"code": "CS401", "credits": 4}], "total_credits": 4}
    ]
  },
  "replan": {
    "source_plan_id": "64adff786f4c",
    "elapsed_semesters": 2,
    "completed_courses": ["CS101", "CS101L", "CS201", "CS301", "MATH103"],
    "difference": {
      "semesters": [
        {"semester": "2027-SPRING", "added_courses": [], "removed_courses": ["CS301"]},
        {"semester": "2027-FALL", "added_courses": [], "removed_courses": []}
      ],
      "added_courses": [],
      "removed_courses": ["CS301"]
    }
  }
}
```

- `request`：本次未来课表的有效规划输入（起始学期已顺延、学期数为剩余数）。
- `replan.difference`：新课表与**原方案剩余学期**（原课表跳过前 2 个已结束学期后按学期对齐）
  的逐学期差异；`added_courses`/`removed_courses` 为跨学期去重汇总。
- 确认草案沿用原约定：`POST /plans/<draft_id>/confirm`；`GET /plans` 列表中草案带
  `source_plan_id` 字段。确认草案不影响原方案。

#### 培养方案达成课表的执行中途重排

对**已确认的培养方案达成课表**（`request.plan_type == "program_plan"`）调用同一接口即可，
请求体不变（只需已结束学期数与累计已修课程）：

- **沿用**原要求版本（`requirement_version_id`）与绑定目录、原每学期学分上限、原期限；
  起始学期顺延 `elapsed_semesters` 个学期，学期数取剩余数。
- **未来排课按当前目录**：开课季节、先修、同修、排课学分与席位余量均以重排时的当前目录
  版本为准（目录修订后即按新版执行），拟修课程与选修归属都可以重新选择。
- **要求归属仍按要求绑定目录**：草案回显 `plan_type="program_plan"`、
  `bound_catalog_version` 与预计归属 `attribution`（结构同
  `POST /requirement-versions/{rv_id}/plans`），门槛学分取绑定目录、学期学分取当前目录。
- **累计已修**必须包含来源申请时声明的已修课；计划外修读的当前目录独有课可作后续
  先修/同修基础但不归属；仅凭累计已修即达成时，草案的未来课表**为空**（仍保存，确认后
  仅撤换来源未来占位），差异中表现为原剩余课表全部移除。
- **拒绝**：来源不是已确认的培养方案达成课表（409）、学期数越界（非正 422 / 达到原期限
  400）、已修回退（422 `COMPLETED_REVOKED`）、未知已修课（422 `COURSE_MISSING`）、
  剩余期限内无解（422 `NO_SOLUTION`）时均说明原因且不保存草案。

```bash
# 来源：POST /requirement-versions/CS-2024:v1/plans 生成并已 confirm 的达成课表
curl -X POST http://localhost:8000/plans/<program_plan_id>/replan \
  -H 'Content-Type: application/json' -d '{
    "elapsed_semesters": 1,
    "completed_courses": ["CS101","CS101L","CS201","CS301","MATH101","MATH103"]
  }'
```

响应（201，节选）：剩余 5 学期自 `2026-FALL` 起，CS401 排入当秋、CS303 顺延明春；
归属随未来课表重新计算：

```json
{
  "plan_id": "f0e1d2c3b4a5",
  "status": "draft",
  "plan_type": "program_plan",
  "catalog_version": 3,
  "bound_catalog_version": 2,
  "requirement_version_id": "CS-2024:v1",
  "source_plan_id": "a91c0f7e2b44",
  "request": {
    "plan_type": "program_plan",
    "requirement_version_id": "CS-2024:v1",
    "bound_catalog_version": 2,
    "completed_courses": ["CS101", "CS101L", "CS201", "CS301", "MATH101", "MATH103"],
    "start_semester": "2026-FALL",
    "max_credits_per_semester": 12,
    "num_semesters": 5,
    "target_courses": ["CS303", "CS401"]
  },
  "result": {
    "completion_semester": "2027-SPRING",
    "semesters": [
      {"semester": "2026-FALL",   "courses": [{"code": "CS401", ...}], "total_credits": 3},
      {"semester": "2027-SPRING", "courses": [{"code": "CS303", ...}], "total_credits": 3}
    ]
  },
  "attribution": {
    "requirement_version_id": "CS-2024:v1",
    "bound_catalog_version": 2,
    "completed_courses": ["CS101", "CS101L", "CS201", "CS301", "MATH101", "MATH103"],
    "result": {"achieved": true, "required": [ ... ], "elective_groups": [ ... ]}
  },
  "replan": {
    "source_plan_id": "a91c0f7e2b44",
    "plan_type": "program_plan",
    "elapsed_semesters": 1,
    "completed_courses": ["CS101", "CS101L", "CS201", "CS301", "MATH101", "MATH103"],
    "difference": {
      "semesters": [
        {"semester": "2027-SPRING", "added_courses": ["CS303"], "removed_courses": []}
      ],
      "added_courses": ["CS303"],
      "removed_courses": []
    }
  }
}
```

确认与普通重排完全一致：`POST /plans/<draft_id>/confirm`——事务内原子复核目录与席位，
仅替换来源方案**尚未结束学期**的占位（已结束学期占位保留），同一来源至多一个生效
重排，重复确认幂等；草案不预占席位。

### 席位上限：设置、查询与余量规划

按**课程代码 + 学期**设置非负席位上限；**不设置即不限额**（默认行为与之前完全一致）。
上限全局生效，不随目录版本重置；设置与查询时课程须存在于**当前目录版本**。

```bash
# 设置 CS401 在 2027-FALL 的席位上限为 1（cap 为非负整数；0 = 该学期不可再占）
curl -X PUT http://localhost:8000/seat-caps/CS401/2027-FALL \
  -H 'Content-Type: application/json' -d '{"cap": 1}'
# -> 200
# {"course_code":"CS401","semester":"2027-FALL","cap":1,"occupied":0,"remaining":1}

# 查询：cap/remaining 为 null 表示不限额
curl http://localhost:8000/seat-caps/CS401/2027-FALL
# -> {"cap":1,"occupied":1,"remaining":0,...}

# 列出全部已设置上限
curl http://localhost:8000/seat-caps
# -> {"seat_caps":[{"course_code":"CS401","semester":"2027-FALL","cap":1}, ...]}
```

设置拒绝路径：

```bash
# 未知当前目录课程
curl -X PUT .../seat-caps/NOPE101/2027-FALL -d '{"cap":1}'
# -> 422 COURSE_MISSING
# 非法学期
curl -X PUT .../seat-caps/CS401/2027秋 -d '{"cap":1}'
# -> 400 INVALID_INPUT
# 上限低于已占用
curl -X PUT .../seat-caps/CS401/2027-FALL -d '{"cap":0}'
# -> 409 CAP_BELOW_OCCUPIED（原上限保持不变）
```

**规划基于当时余量**：席位与先修、同修、互认、锁定等规则共同约束排课。
如下例，第一个方案确认后占掉 CS401 在 2027-FALL 的唯一名额，再次规划时
最早解（2027-FALL）无余位，CS401 仅秋季开课，课表自动顺延到 **2028-FALL**：

```bash
curl -X POST http://localhost:8000/plans -H 'Content-Type: application/json' -d '{
  "target_courses": ["CS401"], "start_semester": "2026-SPRING",
  "max_credits_per_semester": 12, "num_semesters": 6
}'
# -> 201：completion_semester 由 2027-FALL 顺延为 2028-FALL
```

容量不足、所有可行学期都无余位时，规划直接说明原因且**不保存方案**：

```json
{"detail": {"code": "NO_SOLUTION",
 "message": "受席位余量限制，6 个学期内无法排完所需课程：CS401 在 2027-FALL 席位已满（上限 0，已占用 0）"}}
```

注意**待确认方案与重排草案不占位**：两个尚未确认的方案可以排出相同的课表；
席位只在确认成功后占用，因此最终能否成立以**确认时的原子复核**为准。

**确认原子复核、拒绝超卖**：确认在单个写事务内复核目录版本与课表每门课的席位后一次性占位。
若两个方案竞争同一唯一名额，先确认者成功；后确认者得到 409 且方案保持 `proposed`、
原占位不变：

```json
{"detail": {"code": "SEAT_CONFLICT",
 "message": "确认时席位余量已变化，以下课表席位不足，确认被拒绝（原方案与原占位保留）：CS401@2027-FALL（需 1，上限 1，已占用 1，余量 0）",
 "conflicts": [{"course_code":"CS401","semester":"2027-FALL","cap":1,"occupied":1,"requested":1,"remaining":0}]}}
```

**重排确认只替换未结束学期占位**：确认重排草案时，来源方案已结束学期的占位原样保留，
仅删除并替换其未结束学期（晚于 `elapsed_semesters`）的占位；重排规划时这些“自身未来占位”
不计入余量（确认时就会被替换），因此不会与自身冲突。若确认时新课表席位不足，
删除与确认在**同一事务**内整体回滚，原占位恢复。同一来源只能有一个生效重排：

```json
{"detail": {"code": "DRAFT_EXPIRED",
 "message": "来源方案 ... 已有生效的重排结果，同一来源只能有一个生效重排，该草案过期，确认被拒绝"}}
```

**互认按实际修读课程占位**：择修替代课 CS402 时只占 CS402 的名额，CS401 名额不被占用。

### 批次联合排课（本轮新增）

教务处一次提交多份**普通课表申请**。每份申请携带唯一非空 `application_id` 与单份规划
`POST /plans` 的全部字段（含可选 `locked`、`unavailable_semesters`）；批次在**同一当前
目录版本与当时席位余量快照**上联合求解。三级确定性目标依次为：

1. **最大化获得完整课表的申请数**；
2. 再按申请标识升序比较**完成学期**（未入选视为晚于任何完成学期）；
3. 再按申请标识升序比较**入选课表逐学期课程代码序列**（学期内代码升序、再按学期先后；
   未入选视为最末）。

提交（注意：即使部分申请独立无解，只要至少一份可排就返回 201，并在响应中给出未入选
申请的原因；全部都不可排才 422 且不保存）：

```bash
curl -X POST http://localhost:8000/plan-batches \
  -H 'Content-Type: application/json' -d '{
    "applications": [
      {"application_id": "app-2026-001",
       "completed_courses": [], "target_courses": ["CS401"],
       "start_semester": "2026-SPRING",
       "max_credits_per_semester": 12, "num_semesters": 6},
      {"application_id": "app-2026-002",
       "completed_courses": [], "target_courses": ["CS401"],
       "start_semester": "2026-SPRING",
       "max_credits_per_semester": 12, "num_semesters": 6}
    ]
  }'
```

响应（201，节选）——逐申请按标识升序给出结果。下例中 CS401 在 2027-FALL 仅 1 个名额，
两份申请联合排课后 **app-001 占 2027-FALL、app-002 顺延 2028-FALL，均获得完整课表**：

```json
{
  "batch_id": "ab2e1b7811b6",
  "status": "proposed",
  "catalog_version": 1,
  "selected_count": 2,
  "total_applications": 2,
  "applications": [
    {"application_id": "app-2026-001", "selected": true, "plan_id": "fc05c4d25f3b",
     "request": {"target_courses": ["CS401"], "...": "..."},
     "result": {"completion_semester": "2027-FALL",
                "semesters": [ {"semester": "2026-SPRING", "courses": [{"code": "CS101", "...": "..."}]}], "...": "..."}},
    {"application_id": "app-2026-002", "selected": true, "plan_id": "92e8ee428f13",
     "result": {"completion_semester": "2028-FALL", "...": "..."}}
  ]
}
```

未入选申请示例（名额封死、6 学期内只有一份能完整时，标识较大者未入选）：

```json
{"application_id": "app-2026-002", "selected": false,
 "reason_code": "BATCH_CAPACITY",
 "reason": "申请虽可独立排出完整课表，但批次内同课程同学期席位总量不足，未入选（已优先保证完整课表申请数最大化）：CS401@2027-FALL（批次总需求 2，上限 1）"}
```

- **独立无解**的申请其 `reason_code` 为该申请自身原因（`NO_SOLUTION`、
  `REQUIREMENT_CONFLICT` 等），不占用批次席位，也不影响其他申请；
  **参数非法/引用未知课程/标识重复**则不属于逐申请结果，而是**整批拒绝**（422/400）。
- **空课表**（目标均已完成）视为完整方案，直接入选且不占席位，
  `completion_semester` 为 `null`、`semesters` 为 `[]`。

整批确认（单事务原子复核目录版本与**整批席位**——全部入选课表的实际修读需求合并计数，
成功一并占位；**待确认批次不预占席位**）：

```bash
curl -X POST http://localhost:8000/plan-batches/<batch_id>/confirm
```

```json
{"batch_id": "ab2e1b7811b6", "status": "confirmed",
 "catalog_version": 1, "selected_count": 2, "applications": [ {"selected": true, "...": "..."}]}
```

- 目录在批次生成后修订 -> 409 `CATALOG_CHANGED`；任一入选课表席位余量不足
  -> 409 `SEAT_CONFLICT`（`conflicts` 给出 `requested` 为**整批合并需求**、
  `remaining` 为确认时余量）。两者都**整批回滚、原占位不变、绝不超卖**。
- 重复确认**幂等**：已确认批次再次确认直接返回，不重复占位。
- 成员课表（`plan_id`）可 `GET /plans/{id}` 按现有方式回看，确认后随批次一并成为
  `confirmed` 并写入席位占位；但**不得**用 `POST /plans/{id}/confirm` 单独确认
  （409 `BATCH_CONFIRM_REQUIRED`），避免部分占位破坏整批语义。

回看与列表：

```bash
curl http://localhost:8000/plan-batches                       # 批次列表
curl http://localhost:8000/plan-batches/<batch_id>           # 批次详情（含未入选原因）
curl http://localhost:8000/plans/<member_plan_id>            # 成员课表按现有方式回看
```

整批拒绝与无解（均不保存批次）：

```bash
# 申请标识重复 -> 422 INVALID_INPUT（整批拒绝、不保存）
# 任一申请空目标 / 非正学分上限 / 非法学期 / 未知课程 -> 422 或 400（整批拒绝、不保存）
# 全部申请都不可排 -> 422 NO_SOLUTION，detail.applications 给出每份的具体原因，不保存批次
```

可运行示例脚本：

```bash
bash examples/batch_demo.sh
```

### 错误响应

失败时返回相应 HTTP 状态码与原因，不返回部分课表：

```json
{"detail": {"code": "NO_SOLUTION", "message": "受开课学期、先修顺序或每学期学分上限 12 限制，2 个学期内无法排完所需课程"}}
```

| code | HTTP | 含义 |
| --- | --- | --- |
| `INVALID_INPUT` | 400/422 | 非法输入：空目标、非正学分/上限、学期格式错误、重复锁定、锁定已修课、锁定学期超出规划期限、互认组含不存在课程/重复归组/少于两门、重复必修项、选修组门槛非正/候选为空等 |
| `COURSE_MISSING` | 422 | 课程缺失：目标或锁定课程不在当前目录版本中；要求版本的必修/选修候选不在绑定目录版本中；审核/达成课表的已修课程不在绑定目录版本中 |
| `REQUIREMENT_CONFLICT` | 422 | 条件冲突：先修循环、同修课无共同开课学期、同修课不在目录、锁定与开课季节/同修/不可排课约束冲突等 |
| `NO_SOLUTION` | 422 | 无解：期限内无法排完（锁定安排、不可排课学期、学分上限/开课季节/先修顺序限制，含全部互认替代均不可行；席位余量不足时消息含已满的课程学期与上限/已占用） |
| `TOO_COMPLEX` | 422 | 组合或搜索超出内置复杂度上限 |
| `CATALOG_CHANGED` | 409 | 确认（或重排落库）时目录已修订，拒绝且不写入 |
| `PLAN_NOT_CONFIRMED` | 409 | 对未确认方案发起滚动重排 |
| `COMPLETED_REVOKED` | 422 | 累计已修撤销了原方案创建时声明的已修课程 |
| `CAP_BELOW_OCCUPIED` | 409 | 席位上限低于该 (课程,学期) 当前已占用席位，拒绝设置，原上限不变 |
| `SEAT_CONFLICT` | 409 | 确认时原子复核席位发现余量不足，拒绝确认、事务回滚、原方案与原占位保留（不超卖） |
| `DRAFT_EXPIRED` | 409 | 重排草案过期：来源方案不存在/未确认，或同一来源已有生效重排结果 |
| `BATCH_CAPACITY` | 201 内联 | 批次中某申请可独立排出完整课表，但联合席位总量不足，在最大化完整课表数后排他未入选（成员 `reason_code`，附超额课程学期/上限/批次总需求；非顶层错误） |
| `BATCH_CONFIRM_REQUIRED` | 409 | 批次成员课表不得单独确认，须经 `POST /plan-batches/{id}/confirm` 整批确认 |
| `BATCH_BROKEN` | 409 | 批次成员课表不完整（数据被外部修改），拒绝确认 |
| `NOT_FOUND` | 404 | 方案、目录版本、要求版本或审核不存在；要求版本绑定的目录版本不存在 |
| `TOO_COMPLEX` | 422 | 达成审核的归属组合（或排课搜索）超出内置复杂度上限 |

## 项目结构

```
app/
  main.py       # FastAPI 路由与错误处理
  planner.py    # 规划引擎（先修组枚举、互认替代、同修合并、锁定/不可排课/席位余量约束、回溯排课、字典序选优、全部可行课表枚举）
  batch_planner.py # 批次联合排课（逐申请可行课表枚举去重、最大化完整数+完成学期+课表序列三级决胜、批次席位总需求约束）
  replan.py     # 执行中途滚动重排（学期顺延、剩余学期课程差异、席位余量透传）
  audit.py      # 达成审核引擎（必修优先、互认去重、选修组优先级学分最大化、归属序列字典序决胜）
  program_plan.py # 培养方案达成课表（绑定目录管互认/计入学分、当前目录管开课/先修/席位；枚举可达成选课集合后复用排课与审核）
  schemas.py    # 请求模型与校验（含互认组、席位上限、培养方案要求版本、达成审核与达成课表校验）
  db.py         # SQLite 持久化（目录版本、互认组、方案、重排草案、席位 seat_caps/seat_holds、要求版本 requirement_versions、审核 audits、批次 plan_batches；方案 attribution_json 存预计归属、batch_id 关联批次）
  seed_data.py  # 种子课程目录（版本 1）
tests/          # pytest 单元与 API 集成测试（含课程互认组、席位上限/余量/原子确认/重排占位替换、达成审核、培养方案达成课表与两版目录分工、批次联合排课测试）
examples/       # demo.sh 调用示例、batch_demo.sh 批次联合排课示例、catalog_v2.json 目录修订示例、catalog_v3_equiv.json 互认组修订示例、requirement_cs_2024.json 要求版本示例
Dockerfile / docker-compose.yml
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```
