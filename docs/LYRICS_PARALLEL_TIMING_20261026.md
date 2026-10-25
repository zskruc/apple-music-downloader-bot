# 多声部逐字歌词修复 / Parallel lyric timing repair

来源：2026-10-25–26 的维护修复，文档整理于 **2026-10-26，Asia/Shanghai**。
本轮公开仓库在 Python 3.12 下通过 **779 项隔离回归测试**，源码隐私检查无发现。
[源码快照](SOURCE_SNAPSHOT.md)中的较早测试数量保留其历史范围。

## 中文

### 自动流程的改动

原生 TTML 可以按显示顺序交错排列多个声部，同一句歌词也可能以回声形式
重复进入。把它当成单条全局时间轴，会误拒绝合法重叠，或把主唱的词钟借给
稍后进入的声部。本次修复保留原文档行序、正文、行 `key`、`agent` 和原生
`metadata`；渲染不再统一写成同一声部。同声部仍检查开始顺序和原有非重叠
边界，发布前逐行复核身份、正文及本地时间窗口。

存在跨声部重叠时，声学对齐要求独立的逐行音频窗口，不用整曲混合转写替代
这些行的证据。重复叠唱还检查真实匹配锚点是否落在各自的原生进入区间，
并拒绝把同一组词钟当成两个不同进入点的独立证据。没有声部身份的第三方
词钟不能通过展平文本来覆盖多声部参考。旧单声部输入继续走兼容路径。

窗口语言选择有明确优先级：

1. 整个请求的 `alignment_language` 优先，覆盖所有窗口。
2. 没有上述覆盖时，逐行的 `alignment_language` 优先于自动判断。
3. 无明确提示时保留原有单语言行为；混合文字体系中，文字仅为汉字的行
   选择 `zh`，仅为汉字/假名且含假名的行选择 `ja`，拉丁字母、跨文字体系
   混合或未知行使用 `auto`。
   拉丁字母本身不能证明一行是英语。明确指定的语言仍须是受支持的代码。

有效 DTW 观察有时没有可确定末边界的词。经过模型、有限时间值及实际音频
窗口校验后，这类空声学观察可以让其它窗口继续执行，但其覆盖率为零，
不会写入成功 checkpoint，也不能在恢复任务时被当成已完成的词钟。
损坏数据、错误模型或超出真实裁剪范围的时间仍会报错。

质量门槛没有放宽：参考正文必须完整保留，既有文本覆盖率、相似度、未验证
行比例、音频身份和时间窗口检查继续执行。继续处理其它窗口不等于通过
发布校验；仍可能因证据不足而拒绝候选。

### 已核验范围与边界

一次脱敏真实维护验收保留了 **70 行、3 个声部、601 个 cue**。该个案还使用了
针对难行的一次性双模型及左右/侧声道复核；这些人工选择和交叉验算没有
自动集成进队列。它证明该次审核结果，不承诺自动流程能处理所有叠唱录音。

DTW 的相邻锚点给出的是推导出的组边界，不是人工测量的发音末端；缺字的
有界插值和多个模型的一致性也不是逐字准确率的真值。cue 数量不能当作
同等数量的独立实测边界。部署者仍应核对实际录音、叠唱进入点和播放效果。

公开仓库只包含通用修复、合成回归用例和本说明；不包含该次验收的完整歌词、
媒体、模型权重、原始生产报告、日志、账号状态或私有部署配置。

## English

Source: maintenance changes from **2026-10-25–26**; documentation prepared on
**2026-10-26, Asia/Shanghai**. This public source tree passed **779 isolated
regression tests** under Python 3.12 and the source privacy check with no
findings. Earlier test counts in the [source snapshot](SOURCE_SNAPSHOT.md)
retain their historical scope.

### Automated behavior

Native TTML can interleave vocal agents in display order, including repeated
echo entries. Treating all rows as one clock can reject valid overlaps or lend
the lead's timing to a later entry. The repair preserves document order, text,
line `key`, `agent` and native `metadata` through alignment and rendering.
Start order and original non-overlap boundaries remain enforced within each
agent. Publication rechecks each line's identity, text and local time window.

Overlapping agents require independent line-window observations. A mixed
whole-song transcript cannot replace their evidence. Repeated overlapping
phrases also need matched acoustic anchors in their own native entry windows;
the same word clock cannot certify two distinct entries. Unlabelled third-party
clocks cannot replace a parallel reference by flattening its text. Existing
single-agent inputs retain their compatibility path.

Window language selection follows this priority:

1. Request-level `alignment_language` overrides every window.
2. Otherwise, a line's `alignment_language` overrides automatic selection.
3. Without explicit hints, existing monolingual behavior is retained. For
   mixed script families, rows whose letters are all Han use `zh`; Han/kana
   rows containing kana use `ja`. Latin, mixed-family or unknown rows use
   `auto`. Latin script alone does not establish English. Explicit values
   must be supported language codes.

A valid DTW observation can lack bounded lexical units. After model, finite
clock and actual audio-crop checks, an empty acoustic observation can allow
other windows to continue. It has zero coverage, is not saved as a successful
checkpoint, and cannot resume as completed timing evidence. Malformed output,
model mismatches and clocks outside the actual crop still fail.

Quality gates remain in force: complete reference text, coverage, similarity,
unverified-line limits, audio identity and local timing checks. Continuing
other windows does not approve publication; insufficient evidence can still
reject the candidate.

### Evidence and limits

One anonymized real maintenance acceptance retained **70 lines, 3 agents and
601 cues**. Difficult rows also received one-off comparison using two models
and left/right/side-channel audio. Those manual selections and cross-checks
are not integrated into the automatic queue. This is evidence for that
reviewed case, not general acceptance of every overlapping recording.

Adjacent DTW anchors supply derived group boundaries, not measured
pronunciation ends. Bounded interpolation and agreement between models are
not per-word ground truth, and cue counts do not count independently measured
boundaries. Operators still need to check the recording, vocal entries and
playback. The public tree contains generic changes, synthetic regression
fixtures and this description, without full lyrics, media, model weights,
raw production reports/logs, account state or private deployment configuration.
