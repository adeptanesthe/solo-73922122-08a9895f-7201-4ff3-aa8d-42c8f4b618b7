# ABX 盲听校准台（Flask + NumPy）

给录音师比较同一段对白的两版降噪结果：上传两份**已对齐、等长、至多 30 秒的
16 kHz 单声道 PCM16 WAV**，圈出 1～6 段两版均非静音的区间；系统只给 **B** 版乘
一个全片共用的正增益，按**未量化**的增益结果把每段 RMS(B)/RMS(A) 校准到
**0.95～1.05**，同时保证全片不超出 PCM16 范围，取**最接近 1** 的可行增益；无解时
给出数值化原因。校准通过后开题，系统**等概率**选定 X=A 或 X=B，校准音频与答案在
开题时冻结；A/B/X 共用同一个播放器并从**同一播放位置**续听；提交判断后才显示答案。
可在另一页改选区间再开新题，旧题始终用各自冻结的音频判分。圈选页还提供
**B−A 诊听拆分**：用原始 A、B，以 1～3 段参考区上最小二乘拟合的同一组 32 抽头
FIR，把另圈的 1 段诊听区的 `B−A` 分成“固定滤波可解释声”与“剩余声”两条可分别
播放的音轨（详见第 5 节）。

---

## 1. 用 Docker Compose 启动（推荐）

```bash
docker compose up --build
# 打开 http://localhost:5000
# 若 5000 端口被占用： ABX_PORT=8080 docker compose up
```

- 服务监听容器内 `5000`，宿主机端口可用 `ABX_PORT` 覆盖。
- 题目 / 工作区数据保存在命名卷 `abx-data` 挂载的 `/data`（环境变量
  `ABX_DATA_DIR`），重启不丢旧题。
- 镜像基于 `python:3.11-slim`，用 `gunicorn wsgi:app`（1 worker / 8 线程）启动，
  带 `/healthz` 健康检查。
- 仓库已附带一对可试听样音 `samples/sample_A.wav`、`samples/sample_B.wav`
  （各 15 秒，16 kHz mono PCM16，NumPy 合成的“对白感”短语 + 两种降噪风格），
  首页可直接试听、下载，或一键“使用这对样音”。

本地直接跑（不用 Docker）：

```bash
pip install -r requirements.txt
python wsgi.py                 # http://127.0.0.1:5000
# 或 gunicorn --bind 0.0.0.0:5000 wsgi:app
```

重新生成样音：`python samples/make_samples.py`（脚本会自检校准结果）。

---

## 2. 网页操作流程

1. **首页 `/`**：选择/拖入 A、B 两份 WAV（或直接载入样音）。服务端立即校验
   RIFF/WAVE、PCM16（兼容 EXTENSIBLE/PCM 子格式）、16 bit、单声道、16 kHz、
   等长、≤480 000 采样（30 s），错误会逐条中文提示。
2. **圈选页 `/workspaces/<wid>/segments`**：
   - A/B 双波形对齐渲染 + 时间轴；在波形上**横向拖选**即可加段，也可点
     “建议区间”（自动检测两版均非静音的连续区间）或用起止秒输入框精确编辑；
     最少 1 段、最多 6 段。
   - 区间变化后自动校准，展示：采用增益 `g`（含 dB）、RMS 交集上下界、PCM16
     削波上限、每段 RMS A/B、原生 B/A、**未量化校准后 B/A** 与达标标记。
   - 无解时顶部红色面板说明原因，“开题”请求也会被服务端以 `409 infeasible`
     硬拒绝。
3. **ABX 题页 `/questions/<qid>`**：
   - 三个大按钮 **A / B / X**，全页只有一个 `<audio>` 元素；切换时记录
     `currentTime` 与播放状态，新音源 `loadedmetadata` 后 seek 到同一位置并按需
     续播（实测 `5.00s → 5.00s` 精确保持）。
   - 提供各对比段的“跳转 + 段内循环”小芯片；快捷键 `A/B/X` 切音源、空格播放暂停。
   - 选择“X 是 A / X 是 B”并提交后，才渲染绿色/红色结果与“正确答案：X 就是 ?”，
     同时 X 按钮副标题揭示身份；提交一次后锁定，重复提交以第一次判分为准。
4. **题目记录 `/questions`**：列出所有题（段数、增益、状态、对错、所选与答案）。
   未答题只显示“待判断”，列表与详情 API 都不含 `x_is`。
5. **B−A 诊听拆分（圈选页内，试听步骤）**：
   1. 在每段行右侧的「参考 / 诊听 / 未用」开关上，把 **1～3 段标为「参考」**
      （绿色），再把**另外 1 段标为「诊听」**（红色）；诊听区不能与参考区重叠，
      页面提示会实时显示“参考 x/3 段 · 诊听 x/1 段”。
   2. 点击 **“拆分 B−A 两轨”**，卡片内出现两个独立播放器：**固定滤波轨**
      （FIR 可解释的 `B−A`）与**剩余轨**（`B−A` 减去固定滤波），可分别播放、
      反复对比；同时展示 FIR 抽头数、有效方程数、设计矩阵秩（32/32 满秩）、
      参考区 R²/残差与诊听区三轨 RMS。
   3. 若任一 PCM16 试听轨会削波，顶部黄色条给出两轨**共同衰减增益 g**（线性值
      与 dB）；不削波时显示 g=1.000000（+0.000 dB）。
   4. 若提示“拟合矩阵秩不足……请更换参考段”，改用两版均有声音、内容更丰富的
      1～3 段后重新拆分。
   5. 一旦增删段、改时间或切换角色，正在播放的音轨会**立即停止**，旧播放器与
      旧音频 URL 同时失效（服务端返回 404）；重新拆分后才会出现新的两轨。
6. **改选再开题**：题页右上“改选对比段再开新题”回到**同一工作区**的圈选页，可任意
   改段、校准、开新题；每道题在 `data/questions/<qid>/` 下保存自己的
   `a.wav`（原始 A 字节）、`b_calibrated.wav`（开题时渲染冻结）、`x.wav`
   （按开题时的随机答案复制 A 或校准 B）与 `record.json`（含答案、段、增益、判分），
   所以旧题的试听与判分完全不受后来改段影响。

---

## 3. 校准规则（与需求逐条对应）

设第 \(i\) 段的均方根振幅（float64、未量化）为

\[
R_A^{(i)}=\sqrt{\frac1{|S_i|}\sum_{k\in S_i} a_k^2},\qquad
R_B^{(i)}=\sqrt{\frac1{|S_i|}\sum_{k\in S_i} b_k^2},\qquad
r_i=R_B^{(i)}/R_A^{(i)} .
\]

只对 B 乘一个全片共用的正增益 \(g\)。要求

\[
0.95\le g\,r_i\le 1.05
\quad\Longleftrightarrow\quad
g\in\left[\frac{0.95}{r_i},\frac{1.05}{r_i}\right].
\]

因此所有段的 RMS 可行交集为

\[
G_{\rm lo}=\max_i \frac{0.95}{r_i},\qquad
G_{\rm hi,RMS}=\min_i \frac{1.05}{r_i}.
\]

**PCM16 削波上限**按“未量化的 \(gB\) 不得超出 PCM16 数学范围
\([-32768,32767]\)”精确计算（保证 \(g=1\) 对任何合法输入都可行）：

\[
g\le G_{\rm clip}=
\min\left(
\frac{32767}{\max B_{>0}},
\frac{32768}{\max |B_{<0}|}
\right).
\]

总可行区间为 \([G_{\rm lo},\ \min(G_{\rm hi,RMS},G_{\rm clip})]\)。**最接近 1 的
可行增益**就是把 1 夹进该区间：

```text
g* = clamp(1, G_lo, min(G_hi_RMS, G_clip))
```

- \(1<G_{\rm lo}\)：B 整体偏轻，取 \(g=G_{\rm lo}\)（最小提升）；
- \(1>\) 上界：B 整体偏响，取上界；
- 否则 \(g=1\)。

无解的三种 `reason_code`（API 与页面都给中文原因和数值）：

1. `silent_segment`：某段 A 或 B 的 RMS 低于静音阈值（约 −75 dBFS，
   `10^(-75/20)·32768 ≈ 5.83` LSB；数字零必被拦）。
2. `disjoint_rms_windows`：各段增益窗口无交集；指出决定下界（B 相对 A 最轻）与
   上界（B 相对 A 最响）的段号、原生比值和具体 \(g\) 边界。
3. `clipping`：满足 RMS 至少需要 \(g\ge G_{\rm lo}\)，但全片峰值只允许
   \(g\le G_{\rm clip}\)，再大就削波。

开题时用 `np.rint(g·B)` 后写入标准 PCM16 WAV（带防御性 clip，校准通过时不会触发）。
需求要求的比值按**未量化**的 `g·B` 计算并展示；量化后实测比值变化在约 \(10^{-4}\)
量级，仍在区间内。

---

## 4. HTTP / JSON API 摘要

便于脚本化验证（文件上传字段支持 `wav_a/wav_b` 以及 `a/b/file_a/file_b/...`
等别名；两个文件部分也可按顺序兜底识别）：

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /healthz`（或 `/health`） | 健康检查 |
| `POST /upload` / `POST /api/workspaces` | multipart 上传 A/B；表单提交 303 跳转，JSON/`Accept: application/json` 返回 `workspace_id` |
| `POST /samples/use` | 用仓库样音创建工作区（GET 也可，方便直接点链接） |
| `GET /api/workspaces/<wid>` | 元信息、双波形 min/max 峰值、自动非静音建议段 |
| `POST /api/workspaces/<wid>/calibrate` | body `{"segments":[{"start_sample":..,"end_sample":..}]}`（也接受秒字段 `start/end`）；返回完整校准报告，无解时 `feasible:false` + reason |
| `POST /api/workspaces/<wid>/questions` | 同样 body；可行 → `201` 题目（不含答案），无解 → `409 code:infeasible`，段非法 → `400` |
| `POST /api/workspaces/<wid>/audition` | body `{"reference":[{start_sample,end_sample}…1～3段], "monitor":{start_sample,end_sample}}`；拟合 32 抽头 FIR 并生成两轨，返回增益/秩/方程数与不透明音频 URL；秩不足 → `409 code:rank_deficient`，区间非法/重叠 → `400` |
| `POST /api/workspaces/<wid>/audition/invalidate` | 圈选变更时让当前拆分的两轨立即失效（旧 token URL 随即 404） |
| `GET /workspaces/<wid>/audition/<token>/{fixed,residual}` | 本次拆分冻结的固定滤波轨 / 剩余轨 PCM16 WAV；token 随重拆或圈选变更失效 |
| `GET /questions/<qid>` | ABX 页面 |
| `GET /api/questions/<qid>` | 题目详情；**未答时无 `x_is`/`answer`**，已答才返回答案与判分 |
| `GET /questions/<qid>/audio/{A,B,X}` | 冻结的 WAV；`X` 是不暴露身份的不透明 URL |
| `POST /api/questions/<qid>/answer` | `{"choice":"A"|"B"}`（兼容 `answer/judgment/...` 与大小写）；返回 `x_is`、`correct`，重复提交返回首次记录 |
| `GET /questions` / `GET /api/questions` | 历史页 / 列表摘要（未答题不含答案） |

---

## 5. B−A 诊听拆分规则（固定滤波可解释声 vs. 剩余声）

在圈选页对**原始 A、B** 操作，与电平校准互不影响。录音师把圈出的区间分两种角色：

- **参考区**：1～3 段（重叠会先合并，共享样点只计一次）；
- **诊听区**：恰好 1 段，且不得与任何参考区重叠。

记差分信号 \(d_k=B_k-A_k\)。用 A 的当前点及前 31 点（共 \(P=32\) 个输入）预测
\(d_k\)，在**全部参考区**上最小二乘求解**同一组** FIR 系数 \(h\)：

\[
d_k \approx \sum_{j=0}^{P-1} h_j A_{k-j},\qquad
h=\arg\min_h \sum_{k\in\mathcal{R}}\left(d_k-\sum_j h_j A_{k-j}\right)^2 .
\]

**整组方程的剔除规则**：目标点 \(k\) 与它用到的 32 个输入点
\(A[k],A[k-1],\ldots,A[k-31]\)（共 33 点）必须**全部落在参考区并集内**；其中
任一点落进诊听区，就把这一整组（目标行）剔除。诊听区因此完全不参与拟合。
参考区先做区间合并，重叠部分的样点不会被重复计数。

实现（`app/audition.py`）按 256 个目标的固定窗口流式累加正规方程
\(X^\top X\,h=X^\top y\)：热循环里所有缓冲区（窗口掩码、压缩索引、256×32 的
设计子块、32×32 Gram 矩阵等）都只按窗口与抽头数**一次性预分配**，实测参考区
从 2 秒扩到 30 秒时，拟合环节峰值内存恒定（约 112 KiB，不含解码音频本身），
不随参考区总长度增长；只有 32×32 的 Gram 矩阵与 32 维向量被持续累加。

**两轨生成**（全长、与原片等长）：

\[
\text{fixed}_k=\sum_{j=0}^{P-1} h_j A_{k-j},\qquad
\text{residual}_k=d_k-\text{fixed}_k .
\]

滤波沿用原片真实的前序 A 样点；仅当 \(k-j<0\)（片头）时把缺失样点视为零，
即只在片头补零。逐点恒有 `fixed + residual == B − A`（量化前精确，量化为
PCM16 后误差 ≤ 1 LSB）。

**共同防削波增益**：分别对两轨的正、负峰值，按 PCM16 非对称边界
（+32767 / −32768，并考虑 `rint` 四舍六入五成双）求最大可行增益，取两轨、两
方向的最小值且不超过 1。任一条 PCM16 试听轨会削波，就用这**同一个增益衰减
两轨**，页面与 API 都显示该增益（线性值与 dB）；不削波时增益为 1。

**秩不足处理**：方程数少于 32，或 Gram 矩阵（SVD）秩 < 32 时返回
`409 rank_deficient`，页面提示“拟合矩阵秩不足……请更换或增加参考段”，引导选
择两版均有声音、频谱更丰富的区间。

**失效语义**：每次拆分用随机 token 把两轨写到
`data/workspaces/<wid>/audition/<token>/`；重新拆分会替换该目录。任何圈选/角色
变更还会调用失效接口立即删除当前目录，旧 token 的音频 URL 随即 404，前端同时
停掉两个播放器——因此**只有本次圈选算出的两轨可以试听**。

---

## 6. 目录结构

```
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt          # 锁定到交付时实测版本
├── wsgi.py                   # gunicorn / python wsgi.py 入口
├── app/
│   ├── __init__.py           # create_app、错误页、包级 app（flask --app app）
│   ├── wavutil.py            # 手写 RIFF 解析、严格格式校验、PCM16 编码
│   ├── calibration.py        # 段解析、RMS、增益区间/削波/最接近 1 的解、波形与建议段
│   ├── audition.py           # B−A 拆分：流式定内存 32 抽头最小二乘 FIR、两轨渲染与共同防削波增益
│   ├── storage.py            # 工作区/题目/诊听拆分持久化、随机 X、提交判分（原子写）
│   ├── routes.py             # 页面 + JSON API + 音频
│   ├── templates/            # base/index/segments/question/questions/error
│   └── static/{css,js}       # 原生 JS，无 CDN/外网依赖
├── samples/
│   ├── make_samples.py
│   ├── sample_A.wav          # 15 s / 16 kHz / mono / PCM16（480 044 B）
│   └── sample_B.wav
├── scripts/smoke.py          # 对运行中服务的纯标准库黑盒冒烟
└── tests/
    ├── test_app.py           # 21 个 unittest（校验/校准数学/全流程/无答案泄露/X 分布）
    ├── test_audition.py      # 19 个 unittest（FIR 数学/剔除规则/定内存/两轨/增益/API 失效）
    └── browser_walk.mjs      # 可选 Playwright 真浏览器走查脚本
```

---

## 7. 交付前自检：实现了什么 / 实际测到什么

### 已实现（对照原始要求）

- [x] Flask + NumPy；无外部服务、无 CDN，前端纯原生 JS/CSS。
- [x] 上传校验：对齐等长、≤30 s、16 kHz、单声道、PCM16，错误中文可读。
- [x] 圈选 1～6 段两版非静音区间：双波形拖选 + 建议区间 + 精确输入 + 增删/上限。
- [x] 只给 B 一个全片共用正增益；未量化 float64 RMS；每段比值 0.95～1.05。
- [x] 全片 PCM16 边界约束（精确到 −32768/32767），取最接近 1 的可行增益。
- [x] 无解说明原因（静音 / RMS 窗口不相交 / 削波三类，含段号与数值）。
- [x] 校准通过才能开题；服务端对无解开题返回 409 硬拦截。
- [x] 等概率 X=A/B（`secrets.randbelow(2)`），开题时冻结校准音频、X 文件与答案。
- [x] A/B/X 单一 `<audio>`，同位置续听（位置 + 播放状态保持）。
- [x] 提交后才显示答案；未答的 HTML/API/列表均无 `x_is`，X 音频 URL 不透明。
- [x] 改选页可换段开新题；旧题目录快照保证旧音频与首次判分不变。
- [x] **B−A 诊听拆分**：1～3 段参考 + 1 段诊听；用 A 当前点及前 31 点预测
      `B−A`，全参考区最小二乘拟合**同一组 32 抽头 FIR**；目标与 32 个输入点任一
      落进诊听区即整组剔除，重叠参考合并后只计一次。
- [x] **拟合临时内存不随参考区长度增长**：固定 256 目标窗口、全部缓冲区一次性
      预分配（实测 2 s→30 s 参考区，拟合峰值恒为约 112 KiB，不含解码音频）。
- [x] 两轨全长渲染：固定滤波轨沿用原片前序 A 样点、仅片头补零；剩余轨
      `=B−A−fixed`，量化前逐点恒等、量化后误差 ≤1 LSB。
- [x] 任一 PCM16 试听轨会削波则两轨按**同一增益**衰减并显示增益（含 dB）；
      秩不足返回 `409 rank_deficient` 并中文提示更换参考段。
- [x] 改圈选/改角色立即停播，服务端旧 token 音频随即 404；只有本次拆分的两轨
      可试听（另有失效接口在圈选变更时主动退役当前拆分）。
- [x] Dockerfile + Compose（健康检查、命名卷、端口可覆盖）。
- [x] 一对可试听样音（首页播放器/下载/一键载入）。

### 实际跑过的验证（本沙箱，Python 3.11 / Node 22）

1. **单元/接口测试 `python -m unittest discover -s tests`：40/40 通过**
   （原 `test_app.py` 21 个 + 新增 `test_audition.py` 19 个）。原 21 个覆盖
   WAV 往返、立体声/采样率/8-bit/float 拒绝、等长/超长、unity 增益、电平偏移纠正、
   窗口不相交、静音段、削波无解、段数限制、秒/采样键等价、完整 ABX 流程、答案不泄露、
   409 拦截、旧题不变、40 次开题的 X 分布（A 计数落在合理区间）、样音可校准。
   新增 19 个覆盖：已知 FIR 系数恢复、32 输入点整组剔除（含诊听区污染输入窗口）、
   重叠参考只计一次、与暴力 `np.linalg.lstsq` 系数逐元素一致（误差 < 1e-8）、
   恒定信号/方程不足的秩不足、两轨分解与片头补零、正负双向防削波共同增益、
   量化后重构误差 ≤1 LSB、参考段数量/重叠校验、两轨 WAV 可播放、重拆旧 token 404、
   `409 rank_deficient`、400/404 契约与衰减增益上报。
2. **真实 gunicorn（`gunicorn wsgi:app`）HTTP 走查**：
   - `/healthz`、首页、静态资源、样音 200；经典 multipart 表单 `POST /upload`
     返回 303；字段别名 `a/b` 可用。
   - 样音 4 个建议段全部检出；前两段校准 `g=0.98131056` 可行；全 4 段脚本自检
     `g=0.964969`，校准后比值全部落在 0.95～1.05。
   - 下载题内 `b_calibrated.wav` 复核：int16 范围 `[-12124,12003]` 无削波，
     重新按**量化后**样本算比值仍 ≈1.05000/1.03110。
   - 不相交电平时返回 `disjoint_rms_windows` 与具体原因，开题 `409`；7 段 `400`。
   - 字节级确认当次 `x.wav` 与 `a.wav` 或 `b_calibrated.wav` 完全相同但 URL 不暴露；
     重复答题保持第一次 `choice=A`。
   - `scripts/smoke.py` 对运行实例 **12 项检查全部 PASS**。
3. **jsdom 交互测试：原 35 项 + 新增诊听 22 项断言全部 PASS**（真实执行页面
   JS + fetch，注入 fetch 与 canvas/audio 桩）。新增诊听走查覆盖：角色开关与
   “参考 x/3 · 诊听 x/1”提示、未配齐时拆分按钮禁用、真实 POST 拆分、两个
   `<audio>` 播放器与当前 token URL、增益条与秩/抽头/方程数展示、两轨 URL 均
   返回 RIFF、播放中改角色**立即停播**、旧报告隐藏并提示、**旧 token 音频在圈选
   变更后 404**、重叠诊听区 400。
4. **定内存实测（`tracemalloc`，单线程 BLAS）**：把参考区从 2 s 扩到 30 s
   （有效方程 1.6 万→46.4 万组），`fit_fir` 在传入解码数组后的峰值内存恒定为
   **112.1 KiB**（2/5/10/20/30 s 五个长度完全相同）；长度相关的仅解码音频本身
   与渲染期全长 float 数组。30 s 全片拟合耗时约 1.9 s。
5. **真 Chromium + Playwright 走查：29 项全部 PASS**（在无 root 沙箱里用
   `apt-get download` 取依赖 deb 解到用户目录并配 `LD_LIBRARY_PATH` 后启动）：
   首页、样音跳转、**真实 canvas 鼠标拖选新增第 3 段**、加到 6 段后按钮禁用、
   真 WAV 解码时长 15.00 s、暂停态切换 **5.00s→B 5.00s 精确同位置**、播放态
   A 续播并自然推进（→5.88s）、段循环芯片、提交后答案揭示、改选开 Q2、历史页、
   旧 Q1 仍显示冻结结果与“此前已提交”、全程无未捕获 JS 错误。截图人工检查过
   首页/圈选/ABX/结果/历史（并加装用户态中文字体确认中文与波形、数值表格、
   标签布局正常；修复了段号与 A/B 角标重叠的小视觉问题，段号改为底部紫色数字丸）。
6. `pyflakes app wsgi.py tests scripts samples` 干净；前端 JS（含
   `segments.js` 诊听逻辑）与 `browser_walk.mjs` 均通过 `node --check`；
   `docker-compose.yml` 经 PyYAML 解析合法。`scripts/smoke.py` 对运行实例
   12 项检查全部 PASS。

### 已知限制 / 待办（如实说明）

- **沙箱内没有 docker / compose 可执行文件与守护进程，因此没有实际执行
  `docker compose build/up`**；已做的是：Compose YAML 解析通过、Dockerfile 按
  `python:3.11-slim` 标准指令编写、其启动命令 `gunicorn ... wsgi:app` 已在本机
  用同版本 gunicorn 27.0.1 实跑验证。建议在有 Docker 的机器上首跑一次构建确认。
- 定位是**单机单录音师工具**：没有登录/多用户隔离，所有题在 `/questions` 全局列出。
  Compose 固定 1 个 worker + 多线程，答题读改写由进程锁 + 原子替换保护；若自行改成
  多 worker，极端并发下“首次判分”需要进一步加文件锁。
- 静音阈值固定为约 −75 dBFS（`SILENCE_RMS_THRESHOLD`，在 `app/wavutil.py`）；
  区间允许重叠；未做零点吸附、频谱图与题目删除 UI。
- 音频响应未实现 HTTP Range：每份 ≤约 1 MB，整文件返回对本地 ABX 切换/拖动以及
  诊听两轨的播放/拖动足够；若放宽时长或上反向代理，可再补
  Range/`send_file(conditional=True)`。
- 诊听拆分的参考/诊听角色仅存在于圈选页当前会话与服务端当前 token，不随 ABX
  题目冻结（题目只冻结校准音频与答案）；同一工作区重新拆分即替换旧两轨。
  FIR 固定为 32 抽头、拟合窗口固定 256 目标（`app/audition.py` 常量），未做成
  页面可调参数。
- 沙箱基础环境无中文字体，仅影响自动化截图（曾显示方框），已通过用户态安装
  文泉驿微米黑确认真实渲染；终端用户的桌面浏览器通常自带中文字体，不影响功能。
- Playwright/jsdom 工程位于临时目录运行；可复跑的真浏览器脚本已归档到
  `tests/browser_walk.mjs`（需自行 `npm i playwright`），jsdom 脚本未入库。
