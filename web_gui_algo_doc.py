#!/usr/bin/env python3
"""
Malody BPM 测算工作台 —— 独立「算法说明」与「不提供多BPM说明」UI 子模块

设计原则：
1. 独立位于 Web GUI 同级父目录，对 malody_bpm/ 核心测算库保持 100% 零改动；
2. 仅在主工作台顶栏右侧（#openSettingsBtn 左侧）与未导入音频初始空白页（#emptyState）并列注入两枚独立入口按钮：
   - 「BPM、不稳定度算法说明」(#openAlgoDocHeaderBtn / #openAlgoDocEmptyBtn)
   - 「关于程序不提供多BPM的说明」(#openNoMultiBpmHeaderBtn / #openNoMultiBpmEmptyBtn)
   不在 Offset 二级页面（#offsetSecondaryView）放置任何入口；
3. 分别对应两个独立的居中深色 Material 3 模态弹窗：
   - #algoDocModal：顶部三胶囊标签页切换 + 底层硬核规格折叠面板，自然融入 shuoming.txt
   - #noMultiBpmModal：单页四段式递进卡片（含大占比变速灵魂四连反问）+ 底部双重底层数学证明折叠面板，绵里藏针融入 nomultibpm.txt
4. 纯本地离线原生 MathML Core 现代深色学术公式排版，零外部 CDN 与 JS 依赖，严格规避 Jinja2 模板定界符冲突。
"""

from __future__ import annotations

_HEADER_CONTROLS_ANCHOR = (
    '  <div class="controls">\n'
    '    <button type="button" class="header-combo-btn" id="openSettingsBtn"'
)

_EMPTY_STATE_ANCHOR = (
    '    <div class="empty-state" id="emptyState">\n'
    '      <p style="font-size:15px; margin-bottom:6px;">尚未导入音频文件</p>\n'
    '      <p style="font-size:13px;">请从左侧点选曲目，即可查看各模式 BPM 测算结果及自相关峰值图谱</p>\n'
    '    </div>'
)

_BOOK_ICON_SVG = (
    '<svg class="header-btn-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M2.2 3.2c1.6-.5 3.4-.4 4.8.4 1.4-.8 3.2-.9 4.8-.4.4.1.7.5.7.9v8.3c0 .5-.5.8-.9.7-1.4-.4-3-.3-4.6.5-1.6-.8-3.2-.9-4.6-.5-.4.1-.9-.2-.9-.7V4.1c0-.4.3-.8.7-.9z"/>'
    '<path d="M7 3.6v9.5"/>'
    '</svg>'
)

_SINGLE_TRUNK_ICON_SVG = (
    '<svg class="header-btn-icon icon-amber" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M2 8h12"/>'
    '<path d="M4.5 4.2L7.2 8l-2.7 3.8" stroke-dasharray="1.6 1.6"/>'
    '<circle cx="11.2" cy="8" r="2.1"/>'
    '</svg>'
)

_HEADER_ALGO_DOC_BTN_HTML = (
    '    <button type="button" class="btn-algo-doc" id="openAlgoDocHeaderBtn" '
    'title="查看本程序的 BPM 与不稳定度核心算法说明">'
    + _BOOK_ICON_SVG
    + '<span>BPM、不稳定度算法说明</span></button>\n'
    '    <button type="button" class="btn-algo-doc btn-no-multibpm" id="openNoMultiBpmHeaderBtn" '
    'title="查看为什么本程序专注稳态主干而不提供分段多BPM测算">'
    + _SINGLE_TRUNK_ICON_SVG
    + '<span>关于程序不提供多BPM的说明</span></button>'
)

_EMPTY_STATE_REPLACED_HTML = (
    '    <div class="empty-state" id="emptyState">\n'
    '      <p style="font-size:15px; margin-bottom:6px;">尚未导入音频文件</p>\n'
    '      <p style="font-size:13px;">请从左侧点选曲目，即可查看各模式 BPM 测算结果及自相关峰值图谱</p>\n'
    '      <div class="empty-algo-doc-wrap">\n'
    '        <button type="button" class="btn-algo-doc empty-algo-doc-btn" id="openAlgoDocEmptyBtn">'
    + _BOOK_ICON_SVG
    + '<span>BPM、不稳定度算法说明</span></button>\n'
    '        <button type="button" class="btn-algo-doc btn-no-multibpm empty-algo-doc-btn" id="openNoMultiBpmEmptyBtn">'
    + _SINGLE_TRUNK_ICON_SVG
    + '<span>关于程序不提供多BPM的说明</span></button>\n'
    '      </div>\n'
    '      <div class="empty-tips-island" aria-label="温馨提示">\n'
    '        <div class="empty-tips-head">\n'
    '          <span class="empty-tips-dot" aria-hidden="true"></span>\n'
    '          <span>温馨提示：</span>\n'
    '        </div>\n'
    '        <ol class="empty-tips-list">\n'
    '          <li class="empty-tips-item">\n'
    '            <span class="empty-tips-idx" aria-hidden="true">1</span>\n'
    '            <span class="empty-tips-text">一个BPM值的所有 <b>2<sup>n</sup></b> 倍（n为整数），本质上都是同一个值。例：<b>30、60、120、240</b>，本质上都是一样的（三拍子歌曲除外）。且纯自动分析<span class="empty-tip-danger">没有任何办法</span>可以100%准确判定“这首曲子到底是60 BPM还是120 BPM”。</span>\n'
    '          </li>\n'
    '          <li class="empty-tips-item">\n'
    '            <span class="empty-tips-idx" aria-hidden="true">2</span>\n'
    '            <span class="empty-tips-text"><b>Offset</b>（部分游戏里使用 <b>EnterTime</b>、<b>ET</b> 等表述。都是一个意思）分析页面，导入音频后会出现在<b>本页面底部</b>（您可能需要向下滚动）。您无需面对AU、CEP里杂乱的波形图即可轻松入门。</span>\n'
    '          </li>\n'
    # 温馨提示第3项：退出方式说明（2026-11-11 更新：已支持关闭标签页即退出 + 单实例强杀老进程，
    # 故闪退残留进程只需重新运行一次程序即可被新实例清理）。
    '          <li class="empty-tips-item">\n'
    '            <span class="empty-tips-idx" aria-hidden="true">3</span>\n'
    '            <span class="empty-tips-text">点按<b>右上方的退出程序按钮</b>或<b>关闭浏览器标签页</b>均可安全关闭程序（程序有垃圾回收机制）。若不慎<span class="empty-tip-danger">浏览器闪退</span>，会导致后台残留僵尸Python进程。此情况发生时重新运行一次程序并关闭即可（或等待3小时进程自动释放）。</span>\n'
    '          </li>\n'
    # 温馨提示第4项：联系作者（2026-11-11 新增）。
    # 整项用 .empty-tips-contact 高亮显眼；号码为 .contact-chip 按钮，点按复制（复用主页面 copyText），
    # 控制逻辑见 _ALGO_DOC_JS 末尾"联系作者号码点按复制"段。
    # 用语规范：全程序统一使用苹果式"点按"，不用"点击"。
    '          <li class="empty-tips-item empty-tips-contact">\n'
    '            <span class="empty-tips-idx" aria-hidden="true">4</span>\n'
    '            <span class="empty-tips-text">使用中遇到问题、发现测速不准或有功能建议？<b>欢迎联系作者</b>：'
    'QQ<button type="button" class="contact-chip" data-copy="2947568223" title="点按复制 QQ 号">2947568223</button>、'
    '微信<button type="button" class="contact-chip" data-copy="keeponfighting1270" title="点按复制微信号">keeponfighting1270</button>'
    '<span class="contact-hint">（点按号码即可复制）</span></span>\n'
    '          </li>\n'
    '        </ol>\n'
    '      </div>\n'
    '    </div>'
)

# 纯本地离线开方根号矢量 SVG（已升级为原生 MathML Core <msqrt>，保留空串常量以保持兼容）
_RADICAL_SVG = ""

# 纯本地离线行内开方组件（如 √3，采用原生 MathML Core 渲染并带有老旧浏览器纯文本平滑回退）
_SQRT3_HTML = (
    '<math display="inline"><msqrt><mn>3</mn></msqrt></math>'
    '<span class="algo-inline-fallback">&radic;3</span>'
)

# 老旧浏览器不支持 MathML 时的降级占位提示框组件（卡片内就地优雅提示）
_MATHML_FALLBACK_BOX_HTML = (
    '<div class="algo-mathml-fallback" role="alert">'
    '<svg class="algo-fallback-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="8" cy="8" r="7"/>'
    '<line x1="8" y1="5" x2="8" y2="9"/>'
    '<circle cx="8" cy="11.5" r="0.6" fill="currentColor"/>'
    '</svg>'
    '<span>浏览器版本太低，公式无法显示。请更换或升级浏览器。</span>'
    '</div>'
)

# 老旧浏览器不支持 MathML 时的顶部警示横幅组件
_MATHML_TOP_BANNER_HTML = (
    '<div class="algo-doc-mathml-banner" role="status">'
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="8" cy="8" r="7"/>'
    '<line x1="8" y1="5" x2="8" y2="9"/>'
    '<circle cx="8" cy="11.5" r="0.6" fill="currentColor"/>'
    '</svg>'
    '<span>当前浏览器版本过低，不支持 MathML 数学排版，公式已降级占位显示。建议使用 Chrome 109+、Safari、Edge 或 Firefox 浏览器以获得最佳学术阅读体验。</span>'
    '</div>'
)

# 纯本地离线内联 SVG：展示 1000Hz 差分包络平移错位重合 -> ACF 等距谐波峰簇 -> 三点抛物线亚毫秒插值
_ACF_SCHEMATIC_SVG = (
    '<svg class="algo-acf-svg" viewBox="0 0 820 268" xmlns="http://www.w3.org/2000/svg" '
    'role="img" aria-label="自相关错位重合、等距谐波峰簇与三点抛物线亚毫秒插值原理示意图">'
    '<defs>'
    '<linearGradient id="acfPeakGrad" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#f2cc60" stop-opacity="0.42"/>'
    '<stop offset="100%" stop-color="#f2cc60" stop-opacity="0.0"/>'
    '</linearGradient>'
    '<linearGradient id="acfEnvGrad" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0%" stop-color="#58a6ff" stop-opacity="0.32"/>'
    '<stop offset="100%" stop-color="#58a6ff" stop-opacity="0.0"/>'
    '</linearGradient>'
    '</defs>'
    # 背景分栏框：左侧时域与自相关主视图，右侧三点抛物线亚毫秒插值放大视窗
    '<rect x="2" y="2" width="548" height="264" rx="10" fill="#161a22" stroke="#2d3545" stroke-width="1"/>'
    '<rect x="560" y="2" width="258" height="264" rx="10" fill="#161a22" stroke="#3d3424" stroke-width="1.2"/>'
    # 左侧上半区标题（上提至 y=17，避免与下方波形区域及错位抵消标注冲突）
    '<text x="16" y="17" fill="#9ecbff" font-size="12" font-weight="600" font-family="sans-serif">'
    '① 时域错位内积：当平移滞后 τ = k·T（整拍周期）时，主拍尖峰批量对齐共振'
    '</text>'
    # 原始包络 x[t] 基线与脉冲 (t = 65, 175, 285, 395, 505; 切分杂音在 222)
    '<text x="16" y="54" fill="#79c0ff" font-size="11.5" font-weight="600" font-family="monospace">x[t]</text>'
    '<line x1="52" y1="58" x2="534" y2="58" stroke="#222a36" stroke-width="1"/>'
    '<path d="M52,58 L59,58 L65,30 L71,58 L169,58 L175,32 L181,58 '
    'L217,58 L222,41 L227,58 L279,58 L285,29 L291,58 L389,58 L395,33 L401,58 '
    'L499,58 L505,31 L511,58 L534,58" fill="url(#acfEnvGrad)" stroke="#58a6ff" stroke-width="1.7"/>'
    # 平移副本 x[t + τ] 基线与脉冲 (向右平移 1 拍 T=110px: 重拍对齐在 175, 285, 395, 505)
    '<text x="16" y="102" fill="#56d364" font-size="11.5" font-weight="600" font-family="monospace">x[t+τ]</text>'
    '<line x1="52" y1="106" x2="534" y2="106" stroke="#222a36" stroke-width="1"/>'
    '<path d="M52,106 L169,106 L175,78 L181,106 L279,106 L285,80 L291,106 '
    'L327,106 L332,90 L337,106 L389,106 L395,77 L401,106 L499,106 L505,81 L511,106 L534,106" '
    'fill="none" stroke="#56d364" stroke-width="1.7"/>'
    # 平移箭头指示 τ = 1T
    '<line x1="65" y1="70" x2="171" y2="70" stroke="#e3b341" stroke-width="1.3" stroke-dasharray="3,2"/>'
    '<polygon points="174,70 168,67 168,73" fill="#e3b341"/>'
    '<text x="93" y="67" fill="#e3b341" font-size="10.5" font-weight="600" font-family="sans-serif">平移 τ = 1T</text>'
    # 垂直对齐高亮光柱（175, 285, 395, 505 重拍相乘累加；222 切分音错位乘 0 抵消）
    '<line x1="175" y1="30" x2="175" y2="106" stroke="#f2cc60" stroke-width="1.2" stroke-dasharray="2,2" opacity="0.8"/>'
    '<line x1="285" y1="29" x2="285" y2="106" stroke="#f2cc60" stroke-width="1.2" stroke-dasharray="2,2" opacity="0.8"/>'
    '<line x1="395" y1="33" x2="395" y2="106" stroke="#f2cc60" stroke-width="1.2" stroke-dasharray="2,2" opacity="0.8"/>'
    '<line x1="505" y1="31" x2="505" y2="106" stroke="#f2cc60" stroke-width="1.2" stroke-dasharray="2,2" opacity="0.8"/>'
    # 切分音错位指示（从 x[t] 杂音脉冲 y=41 引红色虚线至 x[t+τ] 基线 y=106 零点，标签置于双波形之间 y=86，消除顶部重叠）
    '<line x1="222" y1="44" x2="222" y2="106" stroke="#ff7b72" stroke-width="1.2" stroke-dasharray="2,2" opacity="0.85"/>'
    '<circle cx="222" cy="41" r="2.8" fill="#ff7b72"/>'
    '<circle cx="222" cy="106" r="2.5" fill="#161a22" stroke="#ff7b72" stroke-width="1.2"/>'
    '<rect x="184" y="73" width="76" height="18" rx="4" fill="#1b1214" stroke="#ff7b72" stroke-width="0.8"/>'
    '<text x="222" y="86" fill="#ff7b72" font-size="9.5" font-weight="600" font-family="sans-serif" text-anchor="middle">切分杂音×0抵消</text>'
    # 分隔线
    '<line x1="14" y1="122" x2="536" y2="122" stroke="#1b222c" stroke-width="1"/>'
    # 左侧下半区：自相关曲线 R(τ) 等距谐波峰簇与过原点 OLS 回归斜率
    '<text x="16" y="141" fill="#f2cc60" font-size="12" font-weight="600" font-family="sans-serif">'
    '② 自相关 R(τ) 等距谐波峰簇（1τ, 2τ, 3τ, 4τ...）与过原点 OLS 长基线锁频'
    '</text>'
    '<line x1="52" y1="234" x2="534" y2="234" stroke="#2b3442" stroke-width="1.2"/>'
    '<text x="16" y="232" fill="#f2cc60" font-size="11.5" font-weight="600" font-family="monospace">R(τ)</text>'
    # n//2 截断示意虚线框
    '<rect x="466" y="150" width="68" height="84" fill="#161215" opacity="0.65"/>'
    '<line x1="466" y1="150" x2="466" y2="238" stroke="#ff7b72" stroke-width="1.2" stroke-dasharray="3,3"/>'
    '<text x="472" y="166" fill="#ff7b72" font-size="10" font-weight="600" font-family="sans-serif">τ &gt; N/2</text>'
    '<text x="472" y="180" fill="#8b949e" font-size="9.5" font-family="sans-serif">重叠不足50%</text>'
    '<text x="472" y="193" fill="#8b949e" font-size="9.5" font-family="sans-serif">硬截断防噪</text>'
    # R(τ) 谐波峰曲线 (k=1:145, k=2:235, k=3:325, k=4:415)
    '<path d="M52,234 L62,158 L70,234 L102,231 L108,225 L114,234 '
    'L135,234 L145,162 L155,234 L190,234 L200,228 L225,234 '
    'L235,168 L245,234 L280,232 L315,234 L325,174 L335,234 '
    'L370,233 L405,234 L415,181 L425,234 L465,234" '
    'fill="url(#acfPeakGrad)" stroke="#e3b341" stroke-width="2"/>'
    # 谐波峰顶部圆点与标签
    '<circle cx="145" cy="162" r="3.8" fill="#ffd875" stroke="#090c12" stroke-width="1.2"/>'
    '<circle cx="235" cy="168" r="3.8" fill="#ffd875" stroke="#090c12" stroke-width="1.2"/>'
    '<circle cx="325" cy="174" r="3.8" fill="#ffd875" stroke="#090c12" stroke-width="1.2"/>'
    '<circle cx="415" cy="181" r="3.8" fill="#ffd875" stroke="#090c12" stroke-width="1.2"/>'
    '<text x="134" y="251" fill="#ffd875" font-size="11" font-weight="600" font-family="monospace">1τ</text>'
    '<text x="224" y="251" fill="#ffd875" font-size="11" font-weight="600" font-family="monospace">2τ</text>'
    '<text x="314" y="251" fill="#ffd875" font-size="11" font-weight="600" font-family="monospace">3τ</text>'
    '<text x="404" y="251" fill="#ffd875" font-size="11" font-weight="600" font-family="monospace">4τ</text>'
    '<text x="492" y="251" fill="#8b949e" font-size="10.5" font-family="sans-serif">滞后 τ (ms)</text>'
    # OLS 回归连线示意
    '<line x1="145" y1="154" x2="415" y2="173" stroke="#79c0ff" stroke-width="1.3" stroke-dasharray="4,2"/>'
    '<text x="242" y="156" fill="#79c0ff" font-size="10.5" font-weight="600" font-family="sans-serif">'
    'OLS 线性回归 τ = S_kt / S_kk'
    '</text>'
    # 右侧放大视窗：三点抛物线亚毫秒插值
    '<text x="574" y="22" fill="#ffd875" font-size="12" font-weight="600" font-family="sans-serif">'
    '③ 谐波峰 1ms 网格抛物线插值'
    '</text>'
    '<text x="574" y="39" fill="#8b949e" font-size="10.5" font-family="sans-serif">'
    '突破 1000Hz 整数毫秒采样率限制'
    '</text>'
    # 坐标网格与基线
    '<line x1="582" y1="212" x2="798" y2="212" stroke="#2b3442" stroke-width="1.2"/>'
    '<line x1="630" y1="68" x2="630" y2="212" stroke="#1e2631" stroke-width="1" stroke-dasharray="3,3"/>'
    '<line x1="690" y1="68" x2="690" y2="212" stroke="#1e2631" stroke-width="1" stroke-dasharray="3,3"/>'
    '<line x1="750" y1="68" x2="750" y2="212" stroke="#1e2631" stroke-width="1" stroke-dasharray="3,3"/>'
    # 拟合抛物线曲线 (穿过 c-1: (630,152), c: (690,92), c+1: (750,126)，极值在 x=701, y=89)
    '<path d="M606,196 Q698,10 774,174" fill="none" stroke="#e3b341" stroke-width="2.2"/>'
    # 三个离散整数毫秒采样点
    '<circle cx="630" cy="148" r="4.5" fill="#58a6ff" stroke="#090c12" stroke-width="1.3"/>'
    '<circle cx="690" cy="94" r="4.5" fill="#58a6ff" stroke="#090c12" stroke-width="1.3"/>'
    '<circle cx="750" cy="126" r="4.5" fill="#58a6ff" stroke="#090c12" stroke-width="1.3"/>'
    '<text x="605" y="142" fill="#79c0ff" font-size="10.5" font-family="monospace">y_m1</text>'
    '<text x="668" y="85" fill="#79c0ff" font-size="10.5" font-family="monospace">y_0</text>'
    '<text x="756" y="124" fill="#79c0ff" font-size="10.5" font-family="monospace">y_p1</text>'
    # 真实连续顶点 c + delta
    '<line x1="702" y1="90" x2="702" y2="212" stroke="#56d364" stroke-width="1.5" stroke-dasharray="3,2"/>'
    '<circle cx="702" cy="90" r="5" fill="#56d364" stroke="#090c12" stroke-width="1.4"/>'
    '<text x="709" y="82" fill="#56d364" font-size="11" font-weight="600" font-family="sans-serif">亚毫秒真实顶点</text>'
    '<text x="709" y="96" fill="#56d364" font-size="10.5" font-family="monospace">t = c + δ</text>'
    # X 轴刻度标签
    '<text x="615" y="229" fill="#c9d1d9" font-size="11" font-family="monospace">c-1</text>'
    '<text x="683" y="229" fill="#c9d1d9" font-size="11" font-family="monospace">c</text>'
    '<text x="738" y="229" fill="#c9d1d9" font-size="11" font-family="monospace">c+1</text>'
    # 底部公式说明
    '<rect x="574" y="238" width="230" height="20" rx="5" fill="#121720"/>'
    '<text x="583" y="252" fill="#e6edf3" font-size="10.5" font-family="monospace">'
    'δ = 0.5·(y_p1 - y_m1) / (2y_0 - y_m1 - y_p1)'
    '</text>'
    '</svg>'
)

_ALGO_DOC_CSS = r"""
  /* BPM、不稳定度算法说明 & 不提供多BPM说明按钮与弹窗样式 (web_gui_algo_doc.py) */
  .btn-algo-doc {
    height: 36px;
    padding: 0 14px;
    border-radius: 999px;
    background: #121212;
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    color: var(--text);
    font-size: 14px;
    font-weight: 500;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    transition: background 0.16s ease, color 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
  }
  .btn-algo-doc .header-btn-icon {
    color: var(--accent);
    transition: color 0.16s ease, transform 0.16s ease;
  }
  .btn-algo-doc .header-btn-icon.icon-amber {
    color: #e3b341;
  }
  .btn-algo-doc:hover:not(:disabled) {
    background: #181818;
    border-color: rgba(88, 166, 255, 0.52);
    box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.10);
  }
  .btn-algo-doc.btn-no-multibpm:hover:not(:disabled) {
    border-color: rgba(210, 153, 34, 0.56);
    box-shadow: 0 0 0 3px rgba(210, 153, 34, 0.11);
  }
  .btn-algo-doc:hover:not(:disabled) .header-btn-icon {
    color: #79c0ff;
  }
  .btn-algo-doc.btn-no-multibpm:hover:not(:disabled) .header-btn-icon.icon-amber {
    color: #f2cc60;
  }
  .btn-algo-doc:active:not(:disabled) {
    background: #1e1e1e;
  }
  .empty-algo-doc-wrap {
    margin-top: 18px;
    display: flex;
    justify-content: center;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
  }
  .empty-algo-doc-btn {
    height: 38px;
    padding: 0 18px;
    background: #141414;
    border-color: #2d333b;
    color: #e6edf3;
    font-size: 14px;
  }
  .empty-tips-island {
    margin: 26px auto 0 auto;
    max-width: 720px;
    background: #111318;
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 14px;
    padding: 16px 20px;
    text-align: left;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
  }
  .empty-tips-head {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 14px;
    font-weight: 600;
    color: #f0f6fc;
    margin-bottom: 12px;
  }
  .empty-tips-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--amber);
    box-shadow: 0 0 7px rgba(210, 153, 34, 0.75);
    flex-shrink: 0;
  }
  .empty-tips-list {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin: 0;
    padding: 0;
  }
  .empty-tips-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 10px;
    padding: 10px 13px;
  }
  .empty-tips-idx {
    width: 20px;
    height: 20px;
    margin-top: 2px;
    border-radius: 50%;
    background: rgba(88, 166, 255, 0.15);
    border: 1px solid rgba(88, 166, 255, 0.36);
    color: #79c0ff;
    font-size: 12px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    user-select: none;
  }
  .empty-tips-text {
    font-size: 13.5px;
    line-height: 1.68;
    color: #c9d1d9;
  }
  .empty-tips-text b {
    color: #ffffff;
    font-weight: 600;
  }
  .empty-tip-danger {
    color: #FF0000;
    font-weight: 600;
    margin: 0 2px;
  }
  /* 温馨提示第4项「联系作者」：蓝色描边 + 微光底，比前三项更显眼 */
  .empty-tips-item.empty-tips-contact {
    background: rgba(88, 166, 255, 0.08);
    border-color: rgba(88, 166, 255, 0.45);
    box-shadow: 0 0 14px rgba(88, 166, 255, 0.12);
  }
  /* 可点按复制的号码小标签（button 元素，支持键盘 Tab/Enter） */
  .contact-chip {
    display: inline-block;
    margin: 0 4px;
    padding: 1px 9px;
    border-radius: 999px;
    background: rgba(88, 166, 255, 0.18);
    border: 1px solid rgba(88, 166, 255, 0.6);
    color: #79c0ff;
    font: inherit;
    font-weight: 700;
    line-height: 1.5;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
  }
  .contact-chip:hover,
  .contact-chip:focus-visible {
    background: rgba(88, 166, 255, 0.3);
    border-color: rgba(121, 192, 255, 0.9);
    box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.16);
    outline: none;
  }
  .contact-hint {
    color: var(--muted);
    font-size: 12px;
    margin-left: 2px;
  }

  #algoDocModal,
  #noMultiBpmModal {
    z-index: 1050;
  }
  .modal-dialog.algo-doc-dialog {
    width: min(960px, 100%);
    max-height: 89vh;
    background: #161618; /* 二级模态弹窗统一深空灰底色 */
  }
  .modal-dialog.no-multibpm-dialog {
    width: min(820px, 100%);
    max-height: 89vh;
    background: #161618; /* 二级模态弹窗统一深空灰底色 */
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .no-multibpm-dialog .modal-header,
  .no-multibpm-dialog .algo-doc-header-sub,
  .no-multibpm-dialog .no-multibpm-toc-bar,
  .no-multibpm-dialog .modal-footer {
    flex-shrink: 0;
  }
  .no-multibpm-dialog .algo-doc-body {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding-bottom: 36px; /* 底部安全留白，避免底栏遮挡数学证明 */
  }

  /* 首页不提供多BPM说明弹窗：顶部目录锚点导航条 (#noMultiBpmModal) */
  .no-multibpm-toc-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 10px 24px;
    background: #121214; /* 目录导航条深灰背景 */
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    align-items: center;
  }
  .no-multibpm-toc-label {
    font-size: 12.5px;
    font-weight: 600;
    color: var(--muted);
    margin-right: 4px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .no-multibpm-toc-pill {
    border: 1px solid #30363d;
    background: #111418;
    color: #c9d1d9;
    font-size: 12px;
    font-weight: 500;
    padding: 4px 10px;
    border-radius: 999px;
    cursor: pointer;
    transition: all 0.15s ease;
    user-select: none;
  }
  .no-multibpm-toc-pill:hover {
    background: #1f242c;
    border-color: #58a6ff;
    color: #ffffff;
    transform: translateY(-1px);
  }
  .no-multibpm-toc-pill.proof-pill {
    border-color: rgba(210, 153, 34, 0.45);
    color: #f2cc60;
  }
  .no-multibpm-toc-pill.proof-pill:hover {
    background: rgba(210, 153, 34, 0.18);
    border-color: #e3b341;
    color: #fff2cc;
  }
  .algo-doc-dialog .modal-header h2,
  .no-multibpm-dialog .modal-header h2 {
    font-size: 19px;
  }
  .algo-doc-dialog .m3-btn,
  .no-multibpm-dialog .m3-btn {
    font-size: 14.5px;
  }
  .algo-doc-header-sub {
    padding: 12px 24px 14px 24px;
    background: #131316; /* 算法说明弹窗子顶栏过渡背景 */
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
  }
  .algo-tab-bar {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 4px;
    background: #101012; /* Tab切换区微深灰底板 */
    border: 1px solid #242424;
    border-radius: 6px; /* Tab区外框小圆角(6px)，避免折行时大胶囊圆角视觉畸变 */
    padding: 3px;
  }
  .algo-tab-btn {
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 13.5px;
    font-weight: 500;
    padding: 6px 12px;
    border-radius: 999px;
    cursor: pointer;
    transition: background 0.16s ease, color 0.16s ease, box-shadow 0.16s ease;
  }
  .algo-tab-btn:hover {
    color: var(--text);
  }
  .algo-tab-btn.active {
    background: rgba(88, 166, 255, 0.16);
    color: #79c0ff;
    font-weight: 600;
    box-shadow: inset 0 0 0 1px rgba(88, 166, 255, 0.36);
  }
  /* 自相关 (ACF) 专章 Tab 仅文字高亮（去除未选中默认外框与背景），激活时保持专属高亮 */
  .algo-tab-btn.acf-highlight {
    color: #f0c665;
    background: transparent;
    box-shadow: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .algo-tab-btn.acf-highlight:hover {
    color: #ffe299;
    background: rgba(210, 153, 34, 0.12);
    box-shadow: none;
  }
  .algo-tab-btn.acf-highlight.active {
    background: linear-gradient(135deg, rgba(210, 153, 34, 0.28) 0%, rgba(187, 128, 9, 0.22) 100%);
    color: #ffd875;
    font-weight: 600;
    box-shadow: inset 0 0 0 1.2px rgba(227, 179, 65, 0.72), 0 0 12px rgba(210, 153, 34, 0.18);
  }
  /* 易忽略局限（古典与钢琴曲）Tab 仅文字高亮（去除未选中默认外框与背景），激活时保持专属高亮 */
  .algo-tab-btn.limit-highlight {
    color: #ff948d;
    background: transparent;
    box-shadow: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .algo-tab-btn.limit-highlight:hover {
    color: #ffb4ae;
    background: rgba(248, 81, 73, 0.12);
    box-shadow: none;
  }
  .algo-tab-btn.limit-highlight.active {
    background: linear-gradient(135deg, rgba(248, 81, 73, 0.26) 0%, rgba(182, 35, 36, 0.22) 100%);
    color: #ffaba6;
    font-weight: 600;
    box-shadow: inset 0 0 0 1.2px rgba(255, 123, 114, 0.70), 0 0 12px rgba(248, 81, 73, 0.18);
  }
  .algo-tab-badge {
    font-size: 11px;
    font-weight: 700;
    line-height: 1.2;
    padding: 1.5px 6px;
    border-radius: 999px;
    background: rgba(227, 179, 65, 0.22);
    color: #ffdf8e;
    border: 1px solid rgba(227, 179, 65, 0.45);
  }
  .algo-tab-badge.red-badge {
    background: rgba(248, 81, 73, 0.22);
    color: #ffaba6;
    border-color: rgba(255, 123, 114, 0.45);
  }
  .algo-svg-figure {
    background: #151820; /* SVG自相关图解底色，提升与黑色波形对比 */
    border: 1px solid rgba(210, 153, 34, 0.28);
    border-radius: 12px;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow-x: auto;
  }
  .algo-acf-svg {
    width: 100%;
    min-width: 640px;
    height: auto;
    display: block;
  }
  .algo-svg-caption {
    font-size: 12.8px;
    line-height: 1.55;
    color: var(--muted);
    text-align: center;
  }
  .algo-inline-jump-btn {
    margin-top: 4px;
    align-self: flex-start;
    border: 1px solid rgba(210, 153, 34, 0.42);
    background: rgba(210, 153, 34, 0.12);
    color: #f2cc60;
    font-size: 12.2px;
    font-weight: 600;
    padding: 3px 9px;
    border-radius: 999px;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
  }
  .algo-inline-jump-btn:hover {
    background: rgba(210, 153, 34, 0.24);
    border-color: rgba(227, 179, 65, 0.68);
    color: #ffe299;
  }
  .algo-doc-body {
    padding: 20px 24px 26px 24px;
    gap: 18px;
  }
  .algo-tab-panel {
    display: none;
    flex-direction: column;
    gap: 18px;
  }
  .algo-tab-panel.active {
    display: flex;
  }

  /* 引言金句卡片与正文排版 */
  .algo-quote-card {
    background: linear-gradient(135deg, rgba(88, 166, 255, 0.09) 0%, rgba(18, 22, 28, 0.92) 100%);
    border: 1px solid rgba(88, 166, 255, 0.26);
    border-left: 3.5px solid var(--accent);
    border-radius: 14px;
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .algo-quote-card.amber-accent {
    background: linear-gradient(135deg, rgba(210, 153, 34, 0.10) 0%, rgba(20, 18, 14, 0.94) 100%);
    border-color: rgba(210, 153, 34, 0.28);
    border-left-color: var(--amber);
  }
  .algo-quote-card.red-accent {
    background: linear-gradient(135deg, rgba(248, 81, 73, 0.11) 0%, rgba(22, 14, 16, 0.94) 100%);
    border-color: rgba(248, 81, 73, 0.32);
    border-left-color: #f85149;
  }
  .algo-quote-lead {
    font-size: 15.5px;
    font-weight: 600;
    line-height: 1.72;
    color: #f0f6fc;
  }
  .algo-prose {
    font-size: 14.5px;
    line-height: 1.74;
    color: #c9d1d9;
  }
  .algo-prose b,
  .algo-prose strong {
    color: #f0f6fc;
    font-weight: 600;
  }
  .algo-code-inline {
    font-size: 13.2px;
    padding: 1px 6px;
    border-radius: 5px;
    background: rgba(255, 255, 255, 0.07);
    border: 1px solid rgba(255, 255, 255, 0.09);
    color: #e6edf3;
  }

  /* 分组卡片与网格 */
  .algo-section-card {
    background: #1e2026; /* 弹窗内主分组卡片深空灰调 */
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 16px;
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .algo-section-title {
    font-size: 15.5px;
    font-weight: 600;
    color: #ffffff;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .algo-badge {
    font-size: 12px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 999px;
    line-height: 1.35;
  }
  .algo-badge.blue {
    background: rgba(88, 166, 255, 0.15);
    color: #79c0ff;
    border: 1px solid rgba(88, 166, 255, 0.34);
  }
  .algo-badge.green {
    background: rgba(63, 185, 80, 0.15);
    color: #56d364;
    border: 1px solid rgba(63, 185, 80, 0.34);
  }
  .algo-badge.purple {
    background: rgba(188, 140, 255, 0.15);
    color: #d2a8ff;
    border: 1px solid rgba(188, 140, 255, 0.34);
  }
  .algo-badge.red {
    background: rgba(248, 81, 73, 0.14);
    color: #ff7b72;
    border: 1px solid rgba(248, 81, 73, 0.34);
  }
  .algo-badge.amber {
    background: rgba(210, 153, 34, 0.15);
    color: #e3b341;
    border: 1px solid rgba(210, 153, 34, 0.36);
  }

  /* 管线步骤条 */
  .algo-pipeline-steps {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
    gap: 10px;
  }
  .algo-step-box {
    background: #16181d;
    border: 1px solid #262b33;
    border-radius: 12px;
    padding: 11px 12px;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .algo-step-num {
    font-size: 12px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: 0.4px;
  }
  .algo-step-name {
    font-size: 14px;
    font-weight: 600;
    color: #f0f6fc;
  }
  .algo-step-desc {
    font-size: 13px;
    line-height: 1.55;
    color: var(--muted);
  }

  /* 缺陷列表与模式卡片列表 */
  .algo-list {
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .algo-list li {
    background: #181a1f;
    border: 1px solid #232323;
    border-radius: 11px;
    padding: 11px 14px;
    font-size: 14px;
    line-height: 1.68;
    color: #c9d1d9;
  }
  .algo-list li strong {
    color: #f0f6fc;
  }
  .algo-mode-grid {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .algo-mode-card {
    background: #1c1e24; /* 模式卡片深空灰 */
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 14px;
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .algo-mode-card.enh {
    border-color: rgba(63, 185, 80, 0.35);
    background: linear-gradient(180deg, rgba(63, 185, 80, 0.06) 0%, #1c1e24 100%);
  }
  .algo-mode-card.force {
    border-color: rgba(188, 140, 255, 0.35);
    background: linear-gradient(180deg, rgba(188, 140, 255, 0.06) 0%, #1c1e24 100%);
  }

  /* 纯本地离线原生 MathML Core 现代深色学术公式排版 */
  .algo-math-box {
    background: #151820; /* 离线公式渲染底板，提升对比度 */
    border: 1px solid rgba(88, 166, 255, 0.24);
    border-radius: 12px;
    padding: 16px 20px;
    overflow-x: auto;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    box-sizing: border-box;
  }
  .algo-math-box.purple-border {
    border-color: rgba(188, 140, 255, 0.28);
  }
  .algo-math-badge {
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.3px;
    color: #58a6ff;
    background: rgba(56, 139, 253, 0.12);
    border: 1px solid rgba(56, 139, 253, 0.32);
    border-radius: 20px;
    padding: 3px 14px;
    align-self: flex-start;
  }
  .algo-math-box.purple-border .algo-math-badge {
    color: #bc8cff;
    background: rgba(188, 140, 255, 0.12);
    border-color: rgba(188, 140, 255, 0.32);
  }
  .algo-math-box math {
    font-family: "STIX Two Math", "Cambria Math", "Latin Modern Math", "Times New Roman", math, serif;
    font-size: 20.5px;
    color: #f0f6fc;
    line-height: 1.5;
    letter-spacing: 0.02em;
    max-width: 100%;
    overflow-x: auto;
  }
  .algo-math-condition {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 13.5px;
    color: #8b949e;
    margin-top: 2px;
  }
  .algo-math-condition math {
    font-size: 17px;
    color: #f0f6fc;
  }
  /* MathML 上下标与求和/极限上下限浅青蓝点缀，层次分明 */
  math msub > :last-child,
  math msup > :last-child,
  math msubsup > :not(:first-child),
  math munder > :last-child,
  math mover > :last-child,
  math munderover > :not(:first-child) {
    color: #9ecbff;
  }
  math mo {
    color: #c9d1d9;
    padding: 0 0.15em;
  }
  math mo.algo-op-highlight {
    color: #ff7b72;
    font-weight: 700;
  }
  math mtext {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 0.82em;
    font-weight: 500;
    color: #c9d1d9;
    padding: 0 0.25em;
  }
  /* 正文行内 MathML 样式 */
  math[display="inline"],
  math:not([display="block"]) {
    font-family: "STIX Two Math", "Cambria Math", "Latin Modern Math", "Times New Roman", math, serif;
    font-size: 1.1em;
    color: #f0f6fc;
    vertical-align: -0.15em;
  }

  /* 老旧浏览器 MathML 降级兜底样式 */
  .algo-mathml-fallback {
    display: none;
    align-items: center;
    justify-content: center;
    gap: 9px;
    padding: 14px 18px;
    width: 100%;
    box-sizing: border-box;
    background: rgba(248, 81, 73, 0.08);
    border: 1px dashed rgba(248, 81, 73, 0.35);
    border-radius: 9px;
    color: #ff7b72;
    font-size: 14px;
    font-weight: 500;
    text-align: center;
    user-select: none;
  }
  .algo-fallback-icon {
    width: 18px;
    height: 18px;
    flex-shrink: 0;
    stroke: currentColor;
  }
  .algo-doc-mathml-banner {
    display: none;
    align-items: center;
    gap: 10px;
    background: rgba(210, 153, 34, 0.12);
    border: 1px solid rgba(210, 153, 34, 0.38);
    border-radius: 10px;
    padding: 10px 16px;
    margin-bottom: 16px;
    color: #e3b341;
    font-size: 13.5px;
    line-height: 1.5;
  }
  .algo-doc-mathml-banner svg {
    width: 18px;
    height: 18px;
    flex-shrink: 0;
    stroke: currentColor;
  }
  .algo-inline-fallback {
    display: none;
    font-family: inherit;
    font-style: italic;
    color: #f0f6fc;
  }

  /* 当侦测到老旧浏览器不支持 MathML 时（根容器激活 .no-mathml 类） */
  .no-mathml .algo-math-box math,
  .no-mathml .algo-math-box .algo-math-condition {
    display: none !important;
  }
  .no-mathml .algo-mathml-fallback {
    display: flex !important;
  }
  .no-mathml .algo-doc-mathml-banner {
    display: flex !important;
  }
  .no-mathml math[display="inline"],
  .no-mathml math:not([display="block"]) {
    display: none !important;
  }
  .no-mathml .algo-inline-fallback {
    display: inline !important;
  }

  /* 正文行内公式与段落中的 sub / sup 保护：防撑大行距与分层浅青蓝着色 */
  .algo-doc-body sub,
  .algo-doc-body sup,
  .algo-prose sub,
  .algo-prose sup,
  .algo-sym-item sub,
  .algo-sym-item sup,
  .algo-spec-table sub,
  .algo-spec-table sup,
  .algo-code-inline sub,
  .algo-code-inline sup {
    font-size: 0.72em;
    line-height: 0;
    position: relative;
    vertical-align: baseline;
    color: #9ecbff;
    font-weight: 500;
  }
  .algo-doc-body sup,
  .algo-prose sup,
  .algo-sym-item sup,
  .algo-spec-table sup,
  .algo-code-inline sup {
    top: -0.52em;
  }
  .algo-doc-body sub,
  .algo-prose sub,
  .algo-sym-item sub,
  .algo-spec-table sub,
  .algo-code-inline sub {
    bottom: -0.28em;
  }
  .algo-sym-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 8px 14px;
    padding-top: 2px;
  }
  .algo-sym-item {
    background: #16181d;
    border: 1px solid #242932;
    border-radius: 9px;
    padding: 8px 11px;
    font-size: 13.6px;
    line-height: 1.6;
    color: #c9d1d9;
  }
  .algo-sym-item b {
    color: #79c0ff;
    font-family: "Cambria Math", "Times New Roman", Georgia, serif;
    font-size: 15px;
    margin-right: 4px;
  }
  .algo-note-callout {
    background: rgba(210, 153, 34, 0.10);
    border: 1px solid rgba(210, 153, 34, 0.34);
    border-radius: 11px;
    padding: 12px 15px;
    font-size: 14.2px;
    line-height: 1.7;
    color: #e6edf3;
  }

  /* 底层硬核规格可折叠手风琴 (<details>)：彻底消除 overflow: hidden 导致的 min-height: 0 塌缩与白线 bug */
  details.algo-hardcore-fold {
    background: #151820; /* 规格折叠箱深空灰底板 */
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 13px;
    display: block;
    flex-shrink: 0;
    min-height: fit-content;
    overflow: visible; /* 严禁使用 overflow: hidden，避免 flex 容器内 min-height 塌缩为 0 导致只显示一条白线 */
  }
  details.algo-hardcore-fold > summary {
    list-style: none;
    cursor: pointer;
    padding: 12px 16px;
    font-size: 14px;
    font-weight: 600;
    color: #9ecbff;
    background: #1a202c; /* 折叠条栏底色 */
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    user-select: none;
    border-radius: 12px;
    transition: background 0.15s ease, color 0.15s ease;
  }
  details.algo-hardcore-fold > summary::-webkit-details-marker {
    display: none;
  }
  details.algo-hardcore-fold > summary:hover {
    background: #222b3b;
    color: #cae8ff;
  }
  details.algo-hardcore-fold > summary::after {
    content: "展开 ▾"; /* 2026-11-11：原"展开规格 ▾"精简为"展开 ▾"，与展开态"收起 ▴"对称 */
    font-size: 12.5px;
    font-weight: 500;
    color: var(--muted);
    flex-shrink: 0;
  }
  details.algo-hardcore-fold[open] > summary {
    border-bottom: 1px solid #242932;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
  }
  details.algo-hardcore-fold[open] > summary::after {
    content: "收起 ▴";
    color: #79c0ff;
  }
  .algo-fold-content {
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    font-size: 13.6px;
    line-height: 1.68;
    color: #b1bac4;
    min-height: fit-content;
    overflow: visible;
  }
  details.algo-hardcore-fold::details-content {
    display: block;
  }
  .algo-table-wrap {
    overflow-x: auto;
    border: 1px solid #242932;
    border-radius: 9px;
  }
  .algo-spec-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13.2px;
    text-align: left;
  }
  .algo-spec-table th {
    background: #161b22;
    color: #f0f6fc;
    font-weight: 600;
    padding: 8px 11px;
    border-bottom: 1px solid #242932;
    white-space: nowrap;
  }
  .algo-spec-table td {
    padding: 7px 11px;
    border-bottom: 1px solid #1c2128;
    color: #c9d1d9;
    vertical-align: top;
  }
  .algo-spec-table tr:last-child td {
    border-bottom: none;
  }

  /* 灵魂四连反问条目与折叠子标题 */
  .algo-rebuttal-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 2px;
  }
  .algo-rebuttal-item {
    background: #1c202a; /* 灵魂反问条目深空灰 */
    border: 1px solid #262e3a;
    border-left: 3px solid #58a6ff;
    border-radius: 12px;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .algo-rebuttal-head {
    display: flex;
    align-items: flex-start;
    gap: 9px;
    font-size: 14.6px;
    font-weight: 600;
    color: #f0f6fc;
    line-height: 1.55;
  }
  .algo-rebuttal-idx {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 26px;
    height: 21px;
    padding: 0 6px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    background: rgba(88, 166, 255, 0.16);
    color: #79c0ff;
    border: 1px solid rgba(88, 166, 255, 0.36);
    flex-shrink: 0;
    margin-top: 1px;
  }
  .algo-rebuttal-ans {
    font-size: 14px;
    line-height: 1.7;
    color: #c9d1d9;
    padding-left: 35px;
  }
  @media (max-width: 560px) {
    .algo-rebuttal-ans {
      padding-left: 0;
    }
  }
  .algo-fold-subhead {
    font-size: 14px;
    font-weight: 600;
    color: #e6edf3;
    padding-top: 2px;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .algo-fold-divider {
    height: 1px;
    background: #242932;
    margin: 6px 0;
  }
  /* 行内四分音符矢量图标（2026-11-11）：替代依赖回退字体的 Unicode ♩，
     解决偏小线细、上下错位、与"="挤在一起的问题。尺寸随文字 em 缩放，颜色跟随 currentColor */
  .algo-note-icon {
    display: inline-block;
    width: 0.62em;
    height: 1em;
    vertical-align: -0.16em; /* 符头底部略低于基线，与汉字/数字视觉对齐 */
    margin: 0 0.14em 0 0.1em; /* 右侧留白，使与后方"="间距自然 */
    fill: currentColor;
  }
"""

# 纯本地离线行内四分音符 SVG（2026-11-11）：倾斜椭圆符头 + 右侧符干，样式见 CSS .algo-note-icon
_QUARTER_NOTE_SVG = (
    '<svg class="algo-note-icon" viewBox="0 0 10 16" role="img" aria-label="四分音符">'
    '<ellipse cx="3.7" cy="12.9" rx="3.3" ry="2.35" transform="rotate(-22 3.7 12.9)"/>'
    '<rect x="5.75" y="0.8" width="1.3" height="11.8" rx="0.4"/>'
    '</svg>'
)

_ALGO_DOC_MODAL_HTML = f"""
<!-- BPM、不稳定度算法说明模态弹窗 (web_gui_algo_doc.py) -->
<div class="modal-backdrop" id="algoDocModal" role="dialog" aria-modal="true" aria-labelledby="algoDocModalTitle">
  <div class="modal-dialog algo-doc-dialog">
    <div class="modal-header">
      <h2 id="algoDocModalTitle">BPM、不稳定度算法说明</h2>
      <button type="button" class="modal-close-btn" id="closeAlgoDocModalBtn" title="关闭">✕</button>
    </div>
    <div class="algo-doc-header-sub">
      <div class="algo-tab-bar" role="tablist" aria-label="算法说明章节切换">
        <button type="button" class="algo-tab-btn active" role="tab" aria-selected="true" data-algo-tab="why">设计初衷与原版缺陷</button>
        <button type="button" class="algo-tab-btn acf-highlight" role="tab" aria-selected="false" data-algo-tab="acf">
          <span>核心概念：自相关 (ACF)</span>
          <span class="algo-tab-badge">核心</span>
        </button>
        <button type="button" class="algo-tab-btn" role="tab" aria-selected="false" data-algo-tab="bpm">BPM 测算原理（三模式）</button>
        <button type="button" class="algo-tab-btn" role="tab" aria-selected="false" data-algo-tab="instab">不稳定度公式与解读</button>
        <button type="button" class="algo-tab-btn limit-highlight" role="tab" aria-selected="false" data-algo-tab="limit">
          <span>易忽略局限：古典与钢琴曲</span>
          <span class="algo-tab-badge red-badge">局限</span>
        </button>
      </div>
      <span style="font-size:13px; color:var(--muted);">离线原生架构 · 底层逻辑基于 C/Python 实装核准</span>
    </div>

    <div class="modal-body algo-doc-body" id="algoDocScrollBody">
      {_MATHML_TOP_BANNER_HTML}
      <!-- ==================== Tab 1: 设计初衷与原版缺陷 ==================== -->
      <div class="algo-tab-panel active" data-algo-panel="why" role="tabpanel">
        <div class="algo-quote-card">
          <div class="algo-quote-lead">“任何试图通过‘数拍子、定波形’来定BPM的想法，一定会撞墙……”</div>
          <p class="algo-prose">
            真实音乐混音从来不是干净的节拍器方波：贝斯长音掩蔽、人声抢拍拖腔、切分音错位、弱拍漏击，再加上不同合成器与原声乐器各自的包络起振延迟（Attack Time），会让时域波形上的振幅尖峰在理论节拍点前后剧烈游移甚至成片消失。如果只靠在波形上设个阈值“数鼓点个数除时间”，哪怕中途多算两个切分音、漏掉半拍过门，测出来的速度就会差出十万八千里。
          </p>
        </div>

        <div class="algo-quote-card amber-accent">
          <div class="algo-quote-lead">
            “为什么我要把计算过程弄得这么复杂？很简单，如果是转谱，原曲BPM是114，你在五线谱、简谱上写{_QUARTER_NOTE_SVG}=115、{_QUARTER_NOTE_SVG}=113，都没问题。写{_QUARTER_NOTE_SVG}=110、{_QUARTER_NOTE_SVG}=120，问题也不大。甚至写{_QUARTER_NOTE_SVG}=114.514都不是大问题。但是你要是在写谱器BPM那里工工整整地填下115，那谱写个1分钟按键都对不上了，还怎么玩？……”
          </div>
          <p class="algo-prose">
            纸面乐谱是由演奏者实时控速的弹性指导，而音游写谱器铺设的是<strong>毫秒级刚性等距网格</strong>。算一笔最直观的累积相移账：<span class="algo-code-inline">114 BPM</span> 的单拍周期约为 <span class="algo-code-inline">526.316 ms</span>，一旦填成 <span class="algo-code-inline">115 BPM</span>（<span class="algo-code-inline">521.739 ms</span>），虽然单拍只差了不起眼的 <span class="algo-code-inline">4.58 ms</span>，但写满 <strong>1 分钟（114 拍）后网格就会累计超前 <span class="algo-code-inline">521.7 ms</span>——整整错开一整拍</strong>！即便只差 <span class="algo-code-inline">0.1 BPM</span>，一首 3 分钟曲目到尾杀段也会漂移近 <span class="algo-code-inline">90 ms</span>，所有音符与重拍彻底分家。所以本程序必须放弃粗放的局部数拍，用<strong>分频段瞬态包络 + FFT 自相关 + 跨全曲长基线过原点线性回归</strong>，把主干周期死死钉在亚毫秒级精度上。
          </p>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>Malody 原版自动测速算法（Malody模式）的 7 项结构性缺陷</span>
            <span class="algo-badge red">源码级还原诊断</span>
          </div>
          <p class="algo-prose">
            本程序在 <strong>Malody模式</strong> 下 100% 复刻了客户端原版 AutoTiming 的底层管线，同时也正是为了根治原版在复杂曲目上的以下固有短板，才衍生设计了<strong>增强模式</strong>与<strong>强制线性回归模式</strong>：
          </p>
          <ul class="algo-list">
            <li>
              <strong>1. 阶段 3“首次失配即断锁”零容错早断：</strong>原版在自相关（ACF）曲线上按 <span class="algo-code-inline">±8 ms</span> 严苛窗口递推寻峰，<strong>只要中途遇到哪怕 1 拍切分音、弱拍塌陷或局部过门使峰值偏出 ±8 ms（或 ACF ≤ 0），循环当场终止</strong>。若前 3 拍内失配（未满 4 拍门槛）直接报 <span class="algo-code-inline">0.000 BPM</span> 无法计算；若在第 8～10 拍早断，后面几百拍极度稳定的数据会被全部丢弃，短基线回归极易被前几拍微小抖动拉歪斜率。
            </li>
            <li>
              <strong>2. 全曲单次全局 FFT 的留白稀释与超长曲截断盲区：</strong>原版把整首音频一次性送入单个 FFT 算全局自相关，长前奏/尾奏环境铺底、中途慢板抒情段或变速段全部混入内积，严重稀释稳态鼓点峰值信噪比；同时有效窗口仅保留前半程（<span class="algo-code-inline">n // 2</span>），且输入超过 <span class="algo-code-inline">699.05 秒</span>（699,050 ms）会直接硬截断丢弃尾部。
            </li>
            <li>
              <!-- [修改 2026-11-14] 同步原版 >220 BPM 单向亚谐波前瞻导致 220~300 BPM 误折半痛点 -->
              <strong>3. 无条件 &gt;220 BPM 单向亚谐波折半，缺乏 3:2 三连音/三拍子感知：</strong>原版只要胜出候选快于 <span class="algo-code-inline">220 BPM</span>（周期 &lt; 272.73 ms）就无条件往 <span class="algo-code-inline">[110, 220] BPM</span> 寻找整数倍慢周期，而真实曲目因小节级重复几乎必有倍周期候选，导致 <span class="algo-code-inline">220～300 BPM</span> 真高速曲目被一刀切误折半；同时遇到 <span class="algo-code-inline">3/4</span>、<span class="algo-code-inline">6/8</span> 拍或 Swing 摇摆曲目时，极易把 <span class="algo-code-inline">1.5×</span> 附点/三连音伪谐波误锁为主拍，对慢歌强弱拍交替抖动也缺乏双向审视。
            </li>
            <li>
              <strong>4. 阶段 1 候选硬门限（0.7× 衰减 / 1.25× 夺权）与插值除零脆弱性：</strong>原版要求谐波链平均峰高必须超过第 1 拍主峰的 <span class="algo-code-inline">70%</span>，且后续更长周期候选得分必须超过当前霸主 <span class="algo-code-inline">1.25 倍</span> 才能接管，导致强弱拍反差明显的曲目漏失真实基频；此外三点抛物线插值分母为 0 时无保护产生 <span class="algo-code-inline">inf/NaN</span>，直接废除该候选。
            </li>
            <li>
              <strong>5. 双声道直接算术平均混音（相位相消）与单精度滤波累积误差：</strong>原版强制将左右声道线性相加取平均（<span class="algo-code-inline">downmix</span>）后再提包络，面对经过立体声反相拓宽（Haas 效应）的电音合成器或镲片会发生同频反相抵消；级联双二阶滤波器使用 <span class="algo-code-inline">float32</span> 单精度状态机并开启 FTZ 冲零，长曲递归存在微弱舍入累积。
            </li>
            <li>
              <strong>6. 7 级阶梯整数吸附的“双刃剑”误伤：</strong>当早断导致样本偏少、<span class="algo-code-inline">raw_instab</span> 容差偏宽时，原版阶梯量化可能把真实的非整小数 BPM（如 <span class="algo-code-inline">142.08</span> 或磁带微偏版曲目）强行吸附到邻近整数或十分位，造成后半程对不齐。
            </li>
            <li>
              <strong>7. 无中途变速（Tempo Change / a tempo）剥离与散板熔断保护：</strong>原版无法识别并剔除中途加减速过渡段做净主干二次精修；面对纯人声清唱或自由散板片段，只要偶然凑齐 4 个局部噪声峰也会强行报出一个虚假 BPM。
            </li>
          </ul>
        </div>

        <details class="algo-hardcore-fold">
          <summary><span>原版缺陷与本程序增强机制逐项工程映射表</span></summary>
          <div class="algo-fold-content">
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>原版痛点环节</th>
                    <th>Malody模式 (exact) 原生行为</th>
                    <th>增强模式 (enhanced_v3) / 强制模式 (force_v3) 根治方案</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>多声道预处理</td>
                    <td>强制 <span class="algo-code-inline">(L+R)/2</span> 线性混音，<span class="algo-code-inline">float32</span> IIR 状态机</td>
                    <td>默认多声道独立过 4 频段 <span class="algo-code-inline">float64</span> IIR 后求平方能量均值，彻底杜绝反相抵消</td>
                  </tr>
                  <tr>
                    <td>自相关抗干扰</td>
                    <td>单次全曲 FFT（&gt;699.05s 硬截断），留白与变速直接污染峰高</td>
                    <td>固定 <span class="algo-code-inline">15s</span> 窗 / <span class="algo-code-inline">7.5s</span> 步进分窗等权池化 ACF 初筛 + 全曲长程 ACF 二阶精修</td>
                  </tr>
                  <tr>
                    <td>三拍子 / 倍频</td>
                    <td>仅单向将 &gt;220 BPM 倍增周期，无 3:2 判别</td>
                    <!-- [修改 2026-11-13 / 2026-11-14] 补充真波峰加权直方图与分段门控亚谐波前瞻 -->
                    <td>分段门控亚谐波前瞻（&gt;300 BPM 直跳，240～300 BPM 需 15s 分窗脉冲梳对比度中位数 <span class="algo-code-inline">R ≥ 1.10</span>［1.00～2.00 可调］方折半，220～240 BPM 保真）+ 真波峰加权 1ms 相位直方图 <span class="algo-code-inline">1.5τ</span> 对比度检验（<span class="algo-code-inline">R ≥ 1.15</span>）+ 超低速打捞 + 对称 <span class="algo-code-inline">[60, 300]</span> 2^x 折叠</td>
                  </tr>
                  <tr>
                    <td>中途变速净化</td>
                    <td>遇首个 &gt;±8 ms 失配立即断锁停算，无变速感知</td>
                    <td>时域脉冲梳检测连续漏配/重命中边界，满 <span class="algo-code-inline">16.5s</span>（强制模式 <span class="algo-code-inline">8s</span>）净段自动执行无跨缝多主干联合重算</td>
                  </tr>
                  <tr>
                    <td>小数精度保持</td>
                    <td>通过门控即触发 7 级 <span class="algo-code-inline">remainder</span> 阶梯吸附</td>
                    <td>输出未受人工吸附扭曲的连续双精度回归解，保留真实亚毫秒周期</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </details>
      </div>

      <!-- ==================== Tab 2: 核心概念：自相关 (ACF) ==================== -->
      <div class="algo-tab-panel" data-algo-panel="acf" role="tabpanel">
        <div class="algo-quote-card amber-accent">
          <div class="algo-quote-lead">
            “什么是自相关（Autocorrelation, ACF）？一句话概括：把整首曲子的毫秒级起音冲击信号复制一份，让它自己和错位平移 τ 毫秒后的自己逐点相乘再求和——当平移量 τ 恰好等于 1 拍、2 拍、k 拍周期时，全曲成百上千次重拍就会同时‘峰峰相撞’，激发出极其尖锐的等距谐波峰簇。”
          </div>
          <p class="algo-prose">
            在后端计算管线中，<strong>自相关函数（ACF）</strong>是承上启下的灵魂枢纽：前级声学滤波器组负责把复杂的立体声混音提纯为 <strong>1000 Hz（1 采样点 = 1 毫秒）零相位奇对称差分包络</strong> <span class="algo-code-inline">diff_1000hz</span>，而<strong>自相关则负责从这条看似杂乱的时域脉冲序列中，无视起音阈值与首拍位置（Offset），一次性抽取出贯穿全曲的亚毫秒级周期主干</strong>。
          </p>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>一、物理直觉与数学本质：为什么音游测速非用自相关不可？</span>
            <span class="algo-badge amber">无视初相 · 抗切分漏拍</span>
          </div>
          <p class="algo-prose">
            设长度为 <var>N</var>（单位：毫秒）的 1000 Hz 零相位差分起音包络为 <var>x</var>[<var>t</var>]，其在滞后时间（Lag）<var>&tau;</var> 毫秒处的离散非归一化自相关函数 <var>R</var>(<var>&tau;</var>) 定义为原始序列与右移 <var>&tau;</var> 毫秒副本的时域滑动内积：
          </p>

          <div class="algo-math-box" aria-label="离散时域自相关数学定义与谐波共振条件">
            <div class="algo-math-badge">离散时域滑动内积定义与谐波共振条件</div>
            {_MATHML_FALLBACK_BOX_HTML}
            <math display="block">
              <mrow>
                <mi>R</mi>
                <mo>(</mo>
                <mi>&tau;</mi>
                <mo>)</mo>
                <mo>=</mo>
                <munderover>
                  <mo>&sum;</mo>
                  <mrow>
                    <mi>t</mi>
                    <mo>=</mo>
                    <mn>0</mn>
                  </mrow>
                  <mrow>
                    <mi>N</mi>
                    <mo>&minus;</mo>
                    <mn>1</mn>
                    <mo>&minus;</mo>
                    <mi>&tau;</mi>
                  </mrow>
                </munderover>
                <mi>x</mi>
                <mo>[</mo>
                <mi>t</mi>
                <mo>]</mo>
                <mo>&sdot;</mo>
                <mi>x</mi>
                <mo>[</mo>
                <mrow>
                  <mi>t</mi>
                  <mo>+</mo>
                  <mi>&tau;</mi>
                </mrow>
                <mo>]</mo>
              </mrow>
            </math>
            <div class="algo-math-condition">
              <span>全曲同相谐波共振条件：</span>
              <math display="inline">
                <mrow>
                  <mi>&tau;</mi>
                  <mo>&approx;</mo>
                  <mi>k</mi>
                  <mo>&sdot;</mo>
                  <msub>
                    <mi>T</mi>
                    <mtext>beat</mtext>
                  </msub>
                  <mspace width="0.8em"/>
                  <mo>(</mo>
                  <mi>k</mi>
                  <mo>=</mo>
                  <mn>1</mn>
                  <mo>,</mo>
                  <mn>2</mn>
                  <mo>,</mo>
                  <mn>3</mn>
                  <mo>,</mo>
                  <mo>&hellip;</mo>
                  <mo>)</mo>
                </mrow>
              </math>
              <span style="color:#7ee787;">&rarr; 触发全曲同相谐波共振峰</span>
            </div>
          </div>

          <div class="algo-svg-figure">
            {_ACF_SCHEMATIC_SVG}
            <div class="algo-svg-caption">
              ▲ 后端自相关核心机制图解：① 时域差分包络 <span class="algo-code-inline">x[t]</span> 平移整拍周期后主拍同相相乘增强、偶发切分杂音错位归零；② 自相关域 <span class="algo-code-inline">R(τ)</span> 形成等距谐波峰簇并截断后半程 <span class="algo-code-inline">τ &gt; N/2</span> 高方差区；③ 放大单峰周围 3 个整数毫秒点拟合抛物线求取亚毫秒极值 <span class="algo-code-inline">t = c + δ</span>。
            </div>
          </div>

          <ul class="algo-list">
            <li>
              <strong>1. 彻底解耦“BPM 周期”与“Offset 初相”（零相位平移不变性）：</strong>在时域直接找鼓点时，你必须先知道第一拍落在第几毫秒；而在自相关域中，横轴不再是绝对时间 <var>t</var>，而是<strong>相对时间间隔（滞后量 <var>&tau;</var>）</strong>。无论全曲第一声底鼓从 <span class="algo-code-inline">0 ms</span> 还是 <span class="algo-code-inline">143.7 ms</span> 响起，只要两拍之间的间距是 <var>T</var>，它们对 <var>R</var>(<var>T</var>)、<var>R</var>(2<var>T</var>) 的内积贡献就完全恒定——这让后端得以<strong>先在不依赖任何 Offset 假定的前提下把 BPM 锁死到小数点后三位，再反推最佳相位</strong>。
            </li>
            <li>
              <strong>2. 切分音、人声抢拍与弱拍漏击的“统计自净化”：</strong>真实编曲中充斥着偶发的切分重音、鼓手加花（Fill-in）或空拍留白。当序列平移 <var>&tau;</var> = <var>kT</var> 时，孤立的非周期切分尖峰大概率会撞上另一侧平坦的零值区或负半周，乘积 <span class="algo-code-inline">x[t] · x[t+τ] ≈ 0</span> 自动抵消；而贯穿全曲的等距主拍则会发生数百次正正相乘累加，主峰信噪比随曲目长度呈统计量级放大。
            </li>
            <li>
              <strong>3. 倍频谐波峰簇（Harmonic Comb）的长基线误差杠杆：</strong>如果只看第 1 拍滞后峰（<span class="algo-code-inline">1τ</span>），哪怕仅有 <span class="algo-code-inline">0.5 ms</span> 的包络抖动，换算到 BPM 也会产生显著偏差；但自相关会在 <span class="algo-code-inline">1τ, 2τ, 3τ, …, kτ</span> 处生成一整条等距谐波链。追踪到第 <var>k</var> 阶远端谐波峰（例如第 40 拍、第 100 拍）并执行过原点最小二乘回归，<strong>单拍局部测量抖动会被长基线杠杆直接稀释数十至数百倍</strong>。
            </li>
          </ul>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>二、后端 C/FFT 核心计算流与亚毫秒精度提纯算子</span>
            <span class="algo-badge blue">维纳-辛钦定理 · O(N log N)</span>
          </div>
          <p class="algo-prose">
            一首 3 分钟曲目在 1000 Hz 下拥有 <span class="algo-code-inline">N = 180,000</span> 个采样点，若按时域公式直接跑双重循环硬算内积，需要执行逾 <strong>160 亿次</strong>浮点乘加。后端 <span class="algo-code-inline">c_dsp.c :: autocorr_fft_exact_c</span> 通过 5 步纯 C 管线将其压缩至毫秒级，并突破 1 ms 采样率壁垒：
          </p>

          <div class="algo-pipeline-steps">
            <div class="algo-step-box">
              <span class="algo-step-num">ACF 01</span>
              <span class="algo-step-name">n // 2 半程截断</span>
              <span class="algo-step-desc">仅保留重叠度 ≥50% 的前半程滞后 out_len = n // 2，杜绝尾部少样本伪峰</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">ACF 02</span>
              <span class="algo-step-name">2^p 零填充防混叠</span>
              <span class="algo-step-desc">扩展至 next_pow2(n + out_len - 1)（最高 2^20 点），消除循环卷积首尾混叠</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">ACF 03</span>
              <span class="algo-step-name">维纳-辛钦 FFT</span>
              <span class="algo-step-desc">正向基-2 蝶形 FFT 求功率谱 Re²+Im² 后逆变换乘 1/N_pad，瞬间导出全局 ACF</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">ACF 04</span>
              <span class="algo-step-name">三点抛物线插值</span>
              <span class="algo-step-desc">在整数毫秒峰 (c-1, c, c+1) 拟合二次曲线，提取亚毫秒连续顶点 c + δ</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">ACF 05</span>
              <span class="algo-step-name">过原点谐波 OLS</span>
              <span class="algo-step-desc">联立多阶谐波峰精确时延求解 τ = S_kt / S_kk，锁死小数点后三位 BPM</span>
            </div>
          </div>

          <div class="algo-math-box" aria-label="维纳辛钦FFT自相关与三点抛物线插值公式">
            <div class="algo-math-badge">维纳-辛钦 FFT 快速自相关与三点抛物线亚毫秒插值</div>
            {_MATHML_FALLBACK_BOX_HTML}
            <math display="block">
              <mrow>
                <mi>R</mi>
                <mo>(</mo>
                <mi>&tau;</mi>
                <mo>)</mo>
                <mo>=</mo>
                <mfrac>
                  <mn>1</mn>
                  <msub>
                    <mi>N</mi>
                    <mtext>pad</mtext>
                  </msub>
                </mfrac>
                <mo>&sdot;</mo>
                <msup>
                  <mtext>FFT</mtext>
                  <mrow>
                    <mo>&minus;</mo>
                    <mn>1</mn>
                  </mrow>
                </msup>
                <mo>(</mo>
                <msup>
                  <mrow>
                    <mo>|</mo>
                    <mtext>FFT</mtext>
                    <mo>(</mo>
                    <msub>
                      <mi>x</mi>
                      <mtext>pad</mtext>
                    </msub>
                    <mo>)</mo>
                    <mo>|</mo>
                  </mrow>
                  <mn>2</mn>
                </msup>
                <mo>)</mo>
              </mrow>
            </math>
            <div class="algo-math-condition">
              <span>亚毫秒抛物线插值偏移：</span>
              <math display="inline">
                <mrow>
                  <msub>
                    <mi>&delta;</mi>
                    <mi>k</mi>
                  </msub>
                  <mo>=</mo>
                  <mfrac>
                    <mrow>
                      <mn>0.5</mn>
                      <mo>&sdot;</mo>
                      <mo>(</mo>
                      <msub>
                        <mi>y</mi>
                        <mrow>
                          <mi>c</mi>
                          <mo>+</mo>
                          <mn>1</mn>
                        </mrow>
                      </msub>
                      <mo>&minus;</mo>
                      <msub>
                        <mi>y</mi>
                        <mrow>
                          <mi>c</mi>
                          <mo>&minus;</mo>
                          <mn>1</mn>
                        </mrow>
                      </msub>
                      <mo>)</mo>
                    </mrow>
                    <mrow>
                      <mn>2</mn>
                      <msub>
                        <mi>y</mi>
                        <mi>c</mi>
                      </msub>
                      <mo>&minus;</mo>
                      <msub>
                        <mi>y</mi>
                        <mrow>
                          <mi>c</mi>
                          <mo>&minus;</mo>
                          <mn>1</mn>
                        </mrow>
                      </msub>
                      <mo>&minus;</mo>
                      <msub>
                        <mi>y</mi>
                        <mrow>
                          <mi>c</mi>
                          <mo>+</mo>
                          <mn>1</mn>
                        </mrow>
                      </msub>
                    </mrow>
                  </mfrac>
                  <mspace width="0.8em"/>
                  <mo>&rArr;</mo>
                  <mspace width="0.8em"/>
                  <msub>
                    <mi>t</mi>
                    <mi>k</mi>
                  </msub>
                  <mo>=</mo>
                  <msub>
                    <mi>c</mi>
                    <mi>k</mi>
                  </msub>
                  <mo>+</mo>
                  <msub>
                    <mi>&delta;</mi>
                    <mi>k</mi>
                  </msub>
                </mrow>
              </math>
            </div>
          </div>

          <div class="algo-note-callout">
            <strong>为什么必须做零填充（Zero-Padding）与 <span class="algo-code-inline">n // 2</span> 半程截断？</strong><br>
            1. <strong>防循环混叠：</strong>离散傅里叶变换（DFT）天然隐含周期延拓假设，若直接对原长 <var>N</var> 求功率谱逆变换，得到的是首尾相接的<strong>循环自相关</strong>（尾奏鼓点会错误卷绕到前奏与首拍相乘）。后端严格零填充至 <span class="algo-code-inline">n_padded ≥ n + max_lag - 1</span>，确保输出 100% 等价于无偏线性自相关。<br>
            2. <strong>防尾部崩塌：</strong>当平移滞后 <var>&tau;</var> 超过全曲一半（<span class="algo-code-inline">n // 2</span>）时，参与内积的有效重叠区 <span class="algo-code-inline">N - τ</span> 不足 50%，不仅总能量大幅衰减，末尾少量小节的局部噪声还会制造虚假高方差尖峰，因此全模式统一在 <span class="algo-code-inline">out_len = n // 2</span> 处干净截断。
          </div>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>三、三种测算模式在 ACF 架构上的演进与分工对比</span>
            <span class="algo-badge green">从单次全局到分窗池化与梳状投影</span>
          </div>
          <p class="algo-prose">
            虽然三个模式共用底层的 C 原生基-2 FFT 自相关引擎，但为了攻克<strong>长前奏留白稀释、中途切分断锁、真变速污染与散板熔断</strong>等难题，三者在“如何构造和使用 ACF”上采取了截然不同的三代架构：
          </p>

          <div class="algo-mode-grid">
            <div class="algo-mode-card">
              <div class="algo-section-title">
                <span class="algo-badge blue">Malody模式 (exact) · 单次全曲全局 ACF + 刚性逐拍递推</span>
              </div>
              <p class="algo-prose">
                将整首曲目一次性送入单个 FFT 生成全局 <span class="algo-code-inline">acf</span>（若 <span class="algo-code-inline">n + n//2 ≥ 1,048,577</span> 则先硬截断至 <span class="algo-code-inline">699,050 ms</span>）。先在前 <span class="algo-code-inline">4000 ms</span> 内以 <span class="algo-code-inline">±15 ms</span> 半径提取局部极大值作为种子，经阶段 1（<span class="algo-code-inline">±10 ms</span> 谐波链评分）和阶段 2（&gt;220 BPM 单向倍增周期）选出唯一种子周期 <span class="algo-code-inline">best_tau</span>，再于阶段 3 在全局 ACF 上以 <span class="algo-code-inline">±8 ms</span> 严苛窗口逐拍向后递推 OLS 回归（<strong>遇到哪怕单拍峰值偏出 ±8 ms 或 ACF ≤ 0 立即断锁停算</strong>）。
              </p>
            </div>

            <div class="algo-mode-card enh">
              <div class="algo-section-title">
                <span class="algo-badge green">增强模式 (enhanced_v3 · 主力推荐) · 15s/7.5s 分窗池化 ACF + 两阶长程接力 + 净主干联合重算</span>
              </div>
              <p class="algo-prose">
                针对全局 ACF 易被长留白/局部变速稀释的痛点，首创<strong>「短窗等权池化初筛 + 全曲长程精修」两阶接力架构</strong>：先用 <strong>15 秒窗长、7.5 秒步进（50% 重叠）</strong>对各局部窗口独立算 FFT 自相关（截断至 <span class="algo-code-inline">8000 ms</span>）并等权平均生成高信噪比池化序列 <span class="algo-code-inline">acf_pooled</span>，先在 8 秒池化域稳健锁住正确基频，再无缝移交至<strong>全曲长程 ACF</strong> 拉长回归基线 <span class="algo-code-inline">S_kk</span> 锁死亚毫秒斜率。当开启变速净化且最长净稳定段满 <strong>16.5 秒</strong>时，自动剔除变速污染区间，在干净片段上重构<strong>无跨缝分窗池化 ACF</strong>，并提取主/副净段各自独立长程 ACF 执行 <span class="algo-code-inline">0.25x～4x</span> 同源尺度族<strong>多主干联合过原点 OLS 回归</strong>。
              </p>
            </div>

            <div class="algo-mode-card force">
              <div class="algo-section-title">
                <span class="algo-badge purple">强制线性回归模式 (force_v3 · 疑难兜底) · 全局谐波梳能量积分投影 + 余弦渐隐净段合成 acf_clean</span>
              </div>
              <p class="algo-prose">
                <!-- [修改 2026-11-15] 同步 force_v3 0.05ms/0.01ms 分块向量化两级扫描、末端 250ms 线性渐隐、守卫点插值及 5x/3x 奇次分谐波平局消歧 -->
                彻底摒弃“前一拍找不准下一拍就断链”的递推寻峰模式，改为在候选周期 <span class="algo-code-inline">τ ∈ [199.99, 1000.17] ms</span>（对应 <span class="algo-code-inline">59.99～300.01 BPM</span>，先以 <span class="algo-code-inline">0.05 ms</span> 分块向量化全域粗扫、再在峰值 <span class="algo-code-inline">±2%</span> 邻域内以 <strong>0.01 ms 超细步长</strong>加密投影，并在区间外侧各设 1 步守卫点保障边界三点抛物线插值）上向全局 ACF（最长至 <span class="algo-code-inline">30,000 ms</span> 滞后）发射等距谐波梳 <span class="algo-code-inline">kτ</span>，通过<strong>线性插值采样与滞后末端 250 ms 线性渐隐加权能量</strong>（消除阶数跳变台阶）及 <strong>5×/3× 奇次分谐波子集平局消歧（得分 ≥97% 归还基频）</strong>定出周期。开启变速裁剪且最长净段满 <strong>8.0 秒</strong>时，对所有满 <strong>4.0 秒</strong>的净稳定段独立求取 FFT 自相关，在短段末端施加 <span class="algo-code-inline">16～250 ms</span> <strong>汉宁余弦渐隐窗（Cosine Taper）</strong>消除拼接台阶伪峰，无跨缝叠加合成 <span class="algo-code-inline">acf_clean</span>，并复用同一套 <span class="algo-code-inline">0.05 ms → 0.01 ms</span> 加密投影重搜。
              </p>
            </div>
          </div>
        </div>

        <details class="algo-hardcore-fold">
          <summary><span>底层硬核规格：C 原生基-2 蝶形 FFT 旋转因子表、实偶功率谱逆变换与三模式 ACF 参数对照表</span></summary>
          <div class="algo-fold-content">
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>算子 / 参数维度</th>
                    <th>底层 C / Python 实装规格（基于 <span class="algo-code-inline">c_dsp.c</span> &amp; <span class="algo-code-inline">autocorr.py</span> 核准）</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>2<sup>20</sup> 旋转因子与位反转表</td>
                    <td>进程首次调用时通过 <span class="algo-code-inline">dispatch_once / InitOnceExecuteOnce</span> 线程安全初始化容量为 <span class="algo-code-inline">1,048,576 (2²⁰)</span> 的单精度旋转因子表 <span class="algo-code-inline">g_twiddle_re/im</span>（角度步进 <span class="algo-code-inline">-2π / 2²⁰ ≈ -5.992112452678286e-6 rad</span>）与 20-bit 位反转映射表 <span class="algo-code-inline">g_bitrev_20</span>；任意阶数 <span class="algo-code-inline">p ≤ 20</span> 按步长 <span class="algo-code-inline">1 &lt;&lt; (20 - p)</span> 直接查表复用。</td>
                  </tr>
                  <tr>
                    <td>实偶对称功率谱复用正向 FFT</td>
                    <td>正向 FFT 输出复频谱后，原地计算实功率谱密度 <span class="algo-code-inline">in_re[i] = re² + im², in_im[i] = 0.0f</span>；由于实信号功率谱满足严格的<strong>实偶对称性</strong>，其逆傅里叶变换（IFFT）与正向 FFT 完全同构，故直接二次调用 <span class="algo-code-inline">malody_fft_radix2_f32</span> 并乘归一化因子 <span class="algo-code-inline">1.0f / n_padded</span> 即可精确还原时域 ACF。</td>
                  </tr>
                  <tr>
                    <td>699.05 秒单次 FFT 硬件上限</td>
                    <td>单次全局自相关要求零填充长度不超过 <span class="algo-code-inline">2²⁰ = 1,048,576</span>；当输入毫秒数满足 <span class="algo-code-inline">n + n // 2 ≥ 0x100001 (1,048,577)</span> 时，先将包络截断至 <span class="algo-code-inline">0x2AAAA8 // 4 = 699,050</span> 采样点（约 11 分 39 秒），输出前 <span class="algo-code-inline">349,525 ms</span> 滞后。增强模式的分窗池化 ACF 按 15s 滑窗独立调用 FFT，不受此截断限制。</td>
                  </tr>
                  <tr>
                    <td>局部峰提取与边界保护</td>
                    <td><span class="algo-code-inline">find_acf_local_peaks_c</span> 在有效搜索范围 <span class="algo-code-inline">lag ∈ [16, max_lag_cands - 16)</span>（默认 <span class="algo-code-inline">max_lag_cands = min(len(acf), 4000)</span>）内扫描，要求中心点 <span class="algo-code-inline">acf[lag] &gt; 0</span> 且严格大于或等于左右各 <span class="algo-code-inline">±15 ms</span> 邻域内的全部 30 个采样点。</td>
                  </tr>
                  <tr>
                    <td>抛物线插值除零与单侧断链保护</td>
                    <td>三点抛物线插值分母为 <span class="algo-code-inline">denom = (y₀ - y₋₁) + (y₀ - y₊₁)</span>：Malody模式严格复刻原版无条件除法（<span class="algo-code-inline">denom == 0</span> 产生 <span class="algo-code-inline">inf/NaN</span> 触发候选熔断）；增强模式与强制模式内置 <span class="algo-code-inline">denom != 0</span> 及 <span class="algo-code-inline">|δ| ≤ 1.0</span> 有限值守卫，并在净主干二次精修中允许跨越单阶弱奇次谐波塌陷（抗单阶断链）。</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </details>
      </div>

      <!-- ==================== Tab 3: BPM 测算原理（三模式） ==================== -->
      <div class="algo-tab-panel" data-algo-panel="bpm" role="tabpanel">
        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>第一阶段：三模式共享的高精度瞬态包络与自相关前级</span>
            <span class="algo-badge blue">1 ms 时间分辨率</span>
          </div>
          <p class="algo-prose">
            在进入具体模式决策前，音频不会直接在原始波形上找峰值，而是依次经过 5 步声学提纯，将混杂的音乐信号转化为只反映“节奏冲击发生瞬间斜率”的 <strong>1000 Hz（每采样点精确对应 1 毫秒）零相位差分包络</strong>：
          </p>
          <div class="algo-pipeline-steps">
            <div class="algo-step-box">
              <span class="algo-step-num">STEP 01</span>
              <span class="algo-step-name">解码与去静音</span>
              <span class="algo-step-desc">2^n 无损采样率对齐，多声道能量保留及可选首尾静音/孤立爆音剥离</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">STEP 02</span>
              <span class="algo-step-name">4 频段双二阶滤波</span>
              <span class="algo-step-desc">拆分为超低/低/中低/中高 4 个频段独立提取瞬态能量，兼顾底鼓与军鼓镲片</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">STEP 03</span>
              <span class="algo-step-name">中值去噪与对数压缩</span>
              <span class="algo-step-desc">按各频段中值底噪归一化并对数压缩动态范围，补偿群延迟后加权融合</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">STEP 04</span>
              <span class="algo-step-name">1000Hz 零相位差分</span>
              <span class="algo-step-desc">毫秒级重采样后做正反向错位奇对称 IIR 差分，消除相位偏移并凸出起音前沿</span>
            </div>
            <div class="algo-step-box">
              <span class="algo-step-num">STEP 05</span>
              <span class="algo-step-name">基-2 FFT 自相关</span>
              <span class="algo-step-desc">由维纳-辛钦定理经功率谱逆变换求自相关 ACF，将全曲等距节拍转化为谐波峰簇</span>
              <button type="button" class="algo-inline-jump-btn" data-algo-jump-tab="acf">深入阅读 ACF 专章 ➔</button>
            </div>
          </div>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>第二阶段：三种 BPM 测算核心模式原理对比</span>
            <span class="algo-badge green">按需协同诊断</span>
          </div>
          <div class="algo-mode-grid">
            <div class="algo-mode-card">
              <div class="algo-section-title">
                <span class="algo-badge blue">Malody模式 (exact)</span>
                <span>原版三阶段谐波递推 + 7 级阶梯量化</span>
              </div>
              <p class="algo-prose">
                在全局 ACF 前 <span class="algo-code-inline">4000 ms</span> 内提取 <span class="algo-code-inline">±15 ms</span> 局部极大值峰；<strong>阶段 1</strong> 从各峰出发按 <span class="algo-code-inline">±10 ms</span> 窗口递推整数倍谐波并动态更新过原点最小二乘（OLS）周期 <span class="algo-code-inline">τ = S_kt / S_kk</span>，筛选均高 &gt; 首峰 70% 且得分跃升 &gt; 1.25× 的候选；<strong>阶段 2</strong> 对 &gt;220 BPM 候选向 <span class="algo-code-inline">[110, 220] BPM</span> 做亚谐波消歧；<strong>阶段 3</strong> 以 <span class="algo-code-inline">±8 ms</span> 严苛窗口在全曲 ACF 上递推回归（首次失配即停，至少 4 拍），最终通过门控后执行 7 级 <span class="algo-code-inline">IEEE remainder</span> 阶梯吸附。适合复核 Malody 客户端原生测速表现。
              </p>
            </div>

            <div class="algo-mode-card enh">
              <div class="algo-section-title">
                <span class="algo-badge green">增强模式 (enhanced_v3 · 主力推荐)</span>
                <span>分窗池化抗干扰 + 两阶长程 OLS + 3:2 消歧 + 剔除变速多主干联合重算</span>
              </div>
              <p class="algo-prose">
                <!-- [修改 2026-11-13 / 2026-11-14] 补充分段门控亚谐波前瞻、真波峰加权直方图及分段独立 ACF 消偏说明 -->
                先以 <strong>15 秒窗长、7.5 秒步进（50% 重叠）</strong>计算分窗等权池化 ACF（<span class="algo-code-inline">acf_pooled</span>），隔离前奏留白与局部变速污染并突破 699 秒限制；初筛候选执行<strong>分段门控亚谐波前瞻</strong>（&gt;300 BPM 无条件跳向倍周期；240～300 BPM 仅当 15s 分窗时域脉冲梳强弱拍对比度中位数 <span class="algo-code-inline">R ≥ 1.10</span>［可在 1.00～2.00 间调节］时折半；220～240 BPM 原样保留）；随后执行<strong>两阶 OLS 回归</strong>——先在 8 秒池化域稳健锁链（门槛降至 3 拍并附超低速 <span class="algo-code-inline">÷8/÷4/÷2</span> 打捞），再无缝移交至全曲长程 ACF 拉长回归基线锁死小数精度；接着通过<strong>真波峰（<span class="algo-code-inline">peak_only</span>）加权 1ms 相位直方图时域脉冲梳能量对比度</strong>检验 <span class="algo-code-inline">1.5τ</span> 假说以消除长尾底鼓错锁导致的 3:2 三拍子/三连音误判，并按 <span class="algo-code-inline">2^x</span> 对称折叠至 <span class="algo-code-inline">[60, 300] BPM</span>。若出现曲中相位跳变阶跃残差，则在严格护栏（&ge;2 个 &ge;15s 互不重叠独立分段斜率极差 &le;0.08%、修正 &le;&plusmn;0.25%、禁止翻八度）下触发<strong>分段独立 ACF 后置消偏</strong>（豁免单网格梳不稳定度偏斜门控）；若开启变速净化且检测到中途真变速污染，当最长净稳定段满 <strong>16.5 秒</strong>时，自动提取各独立净主干（含满 15 秒副段，可在 8～15 秒间调节）在 <span class="algo-code-inline">0.25x～4x</span> 同源尺度族上执行<strong>多主干联合过原点 OLS 二次精修</strong>并锁定初测八度。
              </p>
            </div>

            <div class="algo-mode-card force">
              <div class="algo-section-title">
                <span class="algo-badge purple">强制线性回归模式 (force_v3 · 疑难兜底)</span>
                <span>零主观先验全局梳状能量投影 + 最优初相对齐 + 0.01ms 加密精修</span>
              </div>
              <p class="algo-prose">
                <!-- [修改 2026-11-15] 同步 force_v3 闭区间边界、0.05ms/0.01ms 两级精扫、末端 250ms 渐隐、奇次消歧与非负三角核初相精修 -->
                彻底抛弃易断链的逐拍递推门限与人耳速度中心偏置，在候选周期 <span class="algo-code-inline">τ ∈ [199.99, 1000.17] ms</span>（对应 <span class="algo-code-inline">59.99～300.01 BPM</span>，采用 <span class="algo-code-inline">0.05 ms</span> 分块向量化粗扫 + 峰值 <span class="algo-code-inline">±2%</span> 内 <strong>0.01 ms 超细加密投影</strong> + 外延守卫点抛物线插值 + 30 秒滞后末端 <span class="algo-code-inline">250 ms</span> 线性渐隐消阶跃 + <span class="algo-code-inline">5×/3×</span> 奇次分谐波平局消歧）上向全局 ACF（最长至 30 秒滞后）发射等距谐波梳进行线性插值加权能量积分，并在非负起音脉冲上以<strong>对称三角核平滑 + 亚毫秒抛物线初相 + 大偏置二次对齐重探</strong>锁定最优相位偏移 <span class="algo-code-inline">φ*</span>。当开启变速裁剪且最长净稳定段满 <strong>8 秒</strong>时，自动对各净段独立求自相关并经余弦渐隐无跨缝合成 <span class="algo-code-inline">acf_clean</span> 执行同等精度二次重搜，并在 <span class="algo-code-inline">2^x</span> 同源时锁定初测主八度。专为切分极重、散板前奏极长导致常规模式熔断的曲目提供客观参考。
              </p>
            </div>
          </div>
        </div>

        <details class="algo-hardcore-fold">
          <summary><span>底层硬核规格 ①：解码采样率 2^n 匹配、声道能量融合与头尾静音裁剪参数</span></summary>
          <div class="algo-fold-content">
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>子模块环节</th>
                    <th>核心工程参数与数学规则</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>采样率无损对齐</td>
                    <td>内置 <span class="algo-code-inline">48000 / 44100 / 32000 Hz</span> 三套精确滤波器表；遇到 <span class="algo-code-inline">96k / 88.2k / 24k / 22.05k / 16k</span> 等非常规采样率时优先按 <span class="algo-code-inline">2^n</span> 整数倍频折算至对应基准率，避免非整数重采样带来的分数相位畸变。</td>
                  </tr>
                  <tr>
                    <td>声道策略差异</td>
                    <td>Malody模式固定整数归一化后算术平均混音（16-bit 乘 <span class="algo-code-inline">1/32768</span>，24-bit 乘 <span class="algo-code-inline">1/8388608</span>）；增强/强制模式默认 <span class="algo-code-inline">auto</span> 保留多声道二维矩阵，在各频段双二阶滤波后按 <span class="algo-code-inline">(1/C)·∑ch²</span> 累加功率，亦支持左/右单边声道独立提取（&gt;2ch 优先取 <span class="algo-code-inline">FL/FR</span>）。</td>
                  </tr>
                  <tr>
                    <td>时长门限与静音裁剪</td>
                    <td>默认校验有效 PCM 时长属于 <span class="algo-code-inline">[45s, 600s]</span>（未解除时长限制时原始解码安全上限 <span class="algo-code-inline">1800s</span>）。头尾静音裁剪基于 <span class="algo-code-inline">10 ms</span> 短窗均方能量相对全曲峰值分贝门限（默认 <span class="algo-code-inline">-50.0 dB</span>，可调 <span class="algo-code-inline">-70～-25 dB</span>），边界外侧各留 <span class="algo-code-inline">30 ms</span> 瞬态保护缓冲；可选剥离与主体间隔 <span class="algo-code-inline">≥0.8s</span>、自身 <span class="algo-code-inline">≤1.5s</span> 的首尾孤立短声岛（单侧最多剥离 2 个，保留主体 <span class="algo-code-inline">≥2.0s</span>），裁剪总长 <span class="algo-code-inline">≥100 ms</span> 时展示去静音胶囊并将变速区间补偿回原始时间轴。</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </details>

        <details class="algo-hardcore-fold">
          <summary><span>底层硬核规格 ②：4 频段双二阶滤波器组、4096-bin 直方图中值、零相位差分与基-2 FFT 截断</span></summary>
          <div class="algo-fold-content">
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>频段 / 算子阶段</th>
                    <th>Stage 索引与级数</th>
                    <th>能量平滑</th>
                    <th>群延迟补偿 (采样点)</th>
                    <th>融合权重 / 核心算子规格</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>Band 0（超低频底鼓）</td>
                    <td>Stage 0～1（2 级双二阶）</td>
                    <td>Stage 12（1 级专用低通）</td>
                    <td><span class="algo-code-inline">-320</span></td>
                    <td>权重 <span class="algo-code-inline">0.30</span></td>
                  </tr>
                  <tr>
                    <td>Band 1（低频）</td>
                    <td>Stage 2～5（4 级双二阶）</td>
                    <td>无单独平滑</td>
                    <td><span class="algo-code-inline">-64</span></td>
                    <td>权重 <span class="algo-code-inline">0.20</span></td>
                  </tr>
                  <tr>
                    <td>Band 2（中低频军鼓）</td>
                    <td>Stage 6～9（4 级双二阶）</td>
                    <td>无单独平滑</td>
                    <td><span class="algo-code-inline">-32</span></td>
                    <td>权重 <span class="algo-code-inline">0.20</span></td>
                  </tr>
                  <tr>
                    <td>Band 3（中高频打击乐）</td>
                    <td>Stage 10～11（2 级双二阶）</td>
                    <td>无单独平滑</td>
                    <td><span class="algo-code-inline">0</span></td>
                    <td>权重 <span class="algo-code-inline">0.30</span></td>
                  </tr>
                  <tr>
                    <td>4096-bin 浮点直方图中值</td>
                    <td colspan="4">无需排序 <span class="algo-code-inline">O(N)</span> 求中值：取 IEEE-754 单精度正浮点机器码 <span class="algo-code-inline">u = (u + 0x40000) &gt;&gt; 19</span> 映射至 <span class="algo-code-inline">[0, 4096]</span> 桶，累加达 <span class="algo-code-inline">N // 2</span> 后左移 19 位还原中值 <span class="algo-code-inline">median</span>；再以 <span class="algo-code-inline">1.0f + v·(2/median)</span> 经浮点指数/尾数位减 <span class="algo-code-inline">0x3f800000</span> 快速逼近 <span class="algo-code-inline">log2</span> 压缩（乘系数 <span class="algo-code-inline">weight × 8.2629583e-8</span>）。</td>
                  </tr>
                  <tr>
                    <td>主平滑与 1000Hz 奇对称差分</td>
                    <td colspan="4">融合包络过 Stage 13～14（2 级低通），按 <span class="algo-code-inline">idx = llrint(m / (1000/sr))</span> 重采样为 1000 Hz；随后以 Stage 15（<span class="algo-code-inline">a1=-1.4, a2=0.48, gain=0.2</span>）分别正算 <span class="algo-code-inline">B[i]</span> 与反算 <span class="algo-code-inline">A[i]</span>，执行错位对减 <span class="algo-code-inline">dst[i] = A[n-2-i] - B[i-1]</span>（等价于 <span class="algo-code-inline">bwd[i+1] - fwd[i-1]</span>），彻底抵消中心项构造纯奇对称零相位导数核。</td>
                  </tr>
                  <tr>
                    <td>基-2 FFT 维纳-辛钦自相关</td>
                    <td colspan="4">若 <span class="algo-code-inline">n + n//2 ≥ 1,048,577 (0x100001)</span>，先截断包络至 <span class="algo-code-inline">699,050</span> 点（约 699.05 秒）；有效输出长度 <span class="algo-code-inline">out_len = n // 2</span>；零填充至 <span class="algo-code-inline">n_padded = next_pow2(n + out_len - 1)</span>（最高 <span class="algo-code-inline">2^20 = 1,048,576</span> 点），执行 <span class="algo-code-inline">float32</span> 基-2 蝶形 FFT 求功率谱 <span class="algo-code-inline">re² + im²</span> 后逆变换并乘 <span class="algo-code-inline">1 / n_padded</span>。</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </details>

        <details class="algo-hardcore-fold">
          <summary><span>底层硬核规格 ③：7 级 IEEE remainder 阶梯量化完整倍率表与 V3 二次精修门限矩阵</span></summary>
          <div class="algo-fold-content">
            <p>
              <strong>A. Malody模式 7 级阶梯谐波量化矩阵（仅在 <span class="algo-code-inline">σ_t ≤ 2.4 ms</span> 且 <span class="algo-code-inline">rel_inst ≤ 5×10⁻⁵</span> 双门控同时满足时启用）：</strong>按从粗到细顺序计算 <span class="algo-code-inline">rem = IEEE_remainder(raw_bpm, 1 / Ratio)</span>，一旦满足 <span class="algo-code-inline">|rem| &lt; Threshold × raw_instab</span> 立即吸附至 <span class="algo-code-inline">round(raw_bpm × Ratio) / Ratio</span> 并返回：
            </p>
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>优先级</th>
                    <th>倍率 Ratio</th>
                    <th>网格步进 (1/Ratio)</th>
                    <th>容差系数 Threshold</th>
                    <th>典型吸附目标示例</th>
                  </tr>
                </thead>
                <tbody>
                  <tr><td>Tier 1</td><td><span class="algo-code-inline">1.0</span></td><td><span class="algo-code-inline">1.000</span>（整数）</td><td><span class="algo-code-inline">3.0</span></td><td>120.000, 148.000, 165.000</td></tr>
                  <tr><td>Tier 2</td><td><span class="algo-code-inline">2.0</span></td><td><span class="algo-code-inline">0.500</span>（半整数）</td><td><span class="algo-code-inline">2.5</span></td><td>122.500, 172.500</td></tr>
                  <tr><td>Tier 3</td><td><span class="algo-code-inline">3.0</span></td><td><span class="algo-code-inline">0.3333...</span>（三分之一）</td><td><span class="algo-code-inline">2.0</span></td><td>133.333, 166.667</td></tr>
                  <tr><td>Tier 4</td><td><span class="algo-code-inline">10.0</span></td><td><span class="algo-code-inline">0.100</span>（十分位）</td><td><span class="algo-code-inline">2.0</span></td><td>175.100, 128.400</td></tr>
                  <tr><td>Tier 5</td><td><span class="algo-code-inline">20.0</span></td><td><span class="algo-code-inline">0.050</span>（二十分位）</td><td><span class="algo-code-inline">1.5</span></td><td>160.050</td></tr>
                  <tr><td>Tier 6</td><td><span class="algo-code-inline">100.0</span></td><td><span class="algo-code-inline">0.010</span>（百分位）</td><td><span class="algo-code-inline">1.5</span></td><td>142.080, 199.610, 201.760</td></tr>
                  <tr><td>Tier 7</td><td><span class="algo-code-inline">200.0</span></td><td><span class="algo-code-inline">0.005</span>（两百分位）</td><td><span class="algo-code-inline">1.0</span></td><td>134.925, 79.995</td></tr>
                </tbody>
              </table>
            </div>
            <p>
              <strong>B. 增强模式与强制线性回归模式的变速边界裁剪及二次重算门限：</strong>
            </p>
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>核心机制</th>
                    <th>增强模式 (enhanced_v3) 规格</th>
                    <th>强制线性回归模式 (force_v3) 规格</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>变速区间识别</td>
                    <td colspan="2">时域脉冲梳匹配容差 <span class="algo-code-inline">±comb_tol</span>（默认 35ms，可调 10～50ms）；连续未命中 <span class="algo-code-inline">≥M</span> 拍（默认 4 拍）判入变速，随后连续命中 <span class="algo-code-inline">≥K</span> 拍（默认 3 拍）判恢复原速（a tempo），并将交界两侧各外扩 <span class="algo-code-inline">Δt</span>（默认 1.0s）纳入剔除区间；只要开启开关且存在非空剔除区间即进入净段二次精修。</td>
                  </tr>
                  <tr>
                    <td>净段二次精修</td>
                    <td>要求最长净稳定段 <span class="algo-code-inline">≥16.5 秒</span>（副段默认 <span class="algo-code-inline">≥15 秒</span>，可调 8～15 秒）：构建无跨缝分窗池化 ACF 重搜，并联合主/副净段独立 ACF 在同源尺度族 <span class="algo-code-inline">(1.0, 2.0, 0.5, 4.0, 0.25)</span> 上执行抗单阶断链多主干联合 OLS 回归（要求尺度折算速度差 <span class="algo-code-inline">≤0.5%</span> 且标准误 <span class="algo-code-inline">SE</span> 单调不劣）。</td>
                    <!-- [修改 2026-11-15] 同步强制模式二次重搜 0.05ms -> 0.01ms、末端 250ms 线性渐隐、奇次消歧与 2^x 同源八度锁定 -->
                    <td>提取长度 <span class="algo-code-inline">≥4.0 秒</span> 的各净稳态段（短段末端施加 <span class="algo-code-inline">16～250ms</span> 汉宁余弦渐隐窗无跨缝叠加合成 <span class="algo-code-inline">acf_clean</span>），当最长净段 <span class="algo-code-inline">≥8.0 秒</span> 时，复用与初测一致的 <span class="algo-code-inline">0.05 ms</span> 分块向量化全域粗扫 + 峰值 <span class="algo-code-inline">±2%</span> 内 <span class="algo-code-inline">0.01 ms</span> 加密投影 + 滞后末端 <span class="algo-code-inline">250 ms</span> 线性渐隐 + 外延守卫点抛物线插值 + <span class="algo-code-inline">5×/3×</span> 奇次分谐波平局消歧，并在 <span class="algo-code-inline">2^x</span> 同源（<span class="algo-code-inline">≤3.5%</span>）且防劣化门控通过时锁定初测主八度。</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </details>
      </div>

      <!-- ==================== Tab 3: 不稳定度公式与解读 ==================== -->
      <div class="algo-tab-panel" data-algo-panel="instab" role="tabpanel">
        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>一、自相关谐波回归不稳定度（Malody模式 &amp; 增强模式通用）</span>
            <span class="algo-badge green">量纲：BPM 级标准差</span>
          </div>
          <p class="algo-prose">
            在 <strong>Malody模式</strong> 与 <strong>增强模式</strong> 中，程序对命中的自相关谐波序列执行过原点最小二乘（OLS）线性回归求取主干 BPM，并同步导出<strong>不稳定度计算公式</strong>：
          </p>

          <div class="algo-math-box" aria-label="自相关线性回归BPM与不稳定度公式">
            <div class="algo-math-badge">自相关谐波线性回归主干速度与不稳定度</div>
            {_MATHML_FALLBACK_BOX_HTML}
            <math display="block">
              <mrow>
                <msub>
                  <mtext>BPM</mtext>
                  <mtext>raw</mtext>
                </msub>
                <mo>=</mo>
                <mfrac>
                  <mrow>
                    <mn>60000</mn>
                    <mo>&sdot;</mo>
                    <msub>
                      <mi>S</mi>
                      <mrow>
                        <mi>k</mi>
                        <mi>k</mi>
                      </mrow>
                    </msub>
                  </mrow>
                  <msub>
                    <mi>S</mi>
                    <mrow>
                      <mi>k</mi>
                      <mi>t</mi>
                    </mrow>
                  </msub>
                </mfrac>
                <mspace width="1.6em"/>
                <mtext>不稳定度</mtext>
                <mo>=</mo>
                <mtext>BPM</mtext>
                <mo>&sdot;</mo>
                <msqrt>
                  <mrow>
                    <mo>(</mo>
                    <mfrac>
                      <mrow>
                        <msub>
                          <mi>S</mi>
                          <mrow>
                            <mi>k</mi>
                            <mi>k</mi>
                          </mrow>
                        </msub>
                        <mo>&sdot;</mo>
                        <msub>
                          <mi>S</mi>
                          <mrow>
                            <mi>t</mi>
                            <mi>t</mi>
                          </mrow>
                        </msub>
                      </mrow>
                      <msubsup>
                        <mi>S</mi>
                        <mrow>
                          <mi>k</mi>
                          <mi>t</mi>
                        </mrow>
                        <mn>2</mn>
                      </msubsup>
                    </mfrac>
                    <mo>&minus;</mo>
                    <mn>1</mn>
                    <mo>)</mo>
                    <mo>&sdot;</mo>
                    <mfrac>
                      <mi>n</mi>
                      <mrow>
                        <mi>n</mi>
                        <mo>&minus;</mo>
                        <mn>1</mn>
                      </mrow>
                    </mfrac>
                  </mrow>
                </msqrt>
              </mrow>
            </math>
          </div>

          <p class="algo-prose"><strong>其中：</strong></p>
          <div class="algo-sym-grid">
            <div class="algo-sym-item">
              <b>k<sub>i</sub></b>代表第 <var>i</var> 个命中的自相关谐波拍序（如第 1, 2, &hellip; 拍；增强模式多主干联合回归中为同源尺度折算拍序 <var>&kappa;<sub>i</sub></var>）。
            </div>
            <div class="algo-sym-item">
              <b>t<sub>i</sub></b>代表第 <var>i</var> 个自相关谐波峰经三点抛物线亚毫秒插值后的精确延迟时间（单位：<span class="algo-code-inline">ms</span>）。
            </div>
            <div class="algo-sym-item">
              <b>n</b>代表成功纳入回归的有效谐波峰总数（Malody模式要求 <var>n</var> &ge; 4，增强模式要求 <var>n</var> &ge; 3）。
            </div>
            <div class="algo-sym-item">
              <b>S<sub>kk</sub>, S<sub>kt</sub>, S<sub>tt</sub></b>分别代表二阶统计累加量 <span class="algo-code-inline">&sum;k<sub>i</sub>&sup2;</span>、<span class="algo-code-inline">&sum;k<sub>i</sub>t<sub>i</sub></span> 与 <span class="algo-code-inline">&sum;t<sub>i</sub>&sup2;</span>。
            </div>
            <div class="algo-sym-item">
              <b>BPM</b>代表当前模式锁定的主干速度基准（Malody模式取阶梯量化前的 <span class="algo-code-inline">raw_bpm</span>，增强模式取 2<sup>x</sup> 折叠后的 <span class="algo-code-inline">final_bpm</span>）。
            </div>
            <div class="algo-sym-item">
              <b>不稳定度</b>代表最终输出的速度回归标准差指标（本工作台保留 3 位小数，Malody 客户端原版界面保留 2 位小数）。
            </div>
          </div>

          <div class="algo-note-callout">
            <strong>核心物理意义与判读边界：</strong>此值越接近 <strong>0</strong>，代表乐曲主干速度越贴近网格、波动越低。<strong>由于自相关回归中明显的变速会被忽略，因此此值无法衡量“是否中途没有变速”。</strong><br>
            <strong>为什么理论上取值范围是 <span class="algo-code-inline">[0, +&infin;)</span>，但实测连达到 <span class="algo-code-inline">0.1</span> 都够呛？</strong>因为自相关谐波递推设有严苛的 <span class="algo-code-inline">&plusmn;8 ms</span>（可调 <span class="algo-code-inline">5～25 ms</span>）窄窗准入门槛——任何偏出窗口的失配拍会当场触发断锁停算，根本不会混入回归样本；而在成功入围的谐波峰中，单拍残差已被限制在数毫秒以内，同时分母中的拍序平方和 <span class="algo-code-inline">S<sub>kk</sub> = &sum;k<sub>i</sub>&sup2; &propto; n&sup3;</span> 随匹配拍数 <var>n</var> 呈立方级增长，会将相对误差成百倍稀释。因此在成功测出 BPM 的正常曲目中，前两个模式的不稳定度常态仅在 <strong><span class="algo-code-inline">0.000～0.050</span></strong> 之间（<strong>能达到 <span class="algo-code-inline">0.100</span> 都极其罕见</strong>；若真遇到大幅波动，程序会直接断链熔断返回兜底值，而不会算出一个很大的数字）。<br>
            <span style="color:#c9d1d9; font-size:13.6px;">提示：若需判断曲目是否存在中途变速、大段散板或长留白，请结合结果卡片下方的<strong>「节拍匹配与占总时长比例」</strong>以及<strong>「变速区域」</strong>诊断信息共同阅读。</span>
          </div>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>二、时域起音归一化不稳定度（强制线性回归模式专用）</span>
            <span class="algo-badge purple">量纲：[0, 1] 四分之一拍比例</span>
          </div>
          <p class="algo-prose">
            由于<strong>强制线性回归模式（force_v3）</strong>不依赖自相关谐波递推，而是在真实差分起音包络上直接铺设周期为 <span class="algo-code-inline">&tau; = 60000 / BPM</span> 的等距节拍网格，因此该模式采用<strong>一阶中心化去偏后的真实起音波峰均方根误差（RMSE）相对四分之一拍（十六分音符跨度 <span class="algo-code-inline">0.25&tau;</span>）的归一化比例</strong>来度量不稳定度：
          </p>

          <div class="algo-math-box purple-border" aria-label="强制线性回归模式RMSE不稳定度公式">
            <div class="algo-math-badge">强制模式 RMSE 抖动残差与归一化不稳定度</div>
            {_MATHML_FALLBACK_BOX_HTML}
            <math display="block">
              <mrow>
                <mtext>RMSE</mtext>
                <mo>=</mo>
                <msqrt>
                  <mrow>
                    <mfrac>
                      <mn>1</mn>
                      <mi>m</mi>
                    </mfrac>
                    <munderover>
                      <mo>&sum;</mo>
                      <mrow>
                        <mi>j</mi>
                        <mo>=</mo>
                        <mn>1</mn>
                      </mrow>
                      <mi>m</mi>
                    </munderover>
                    <msup>
                      <mrow>
                        <mo>(</mo>
                        <msub>
                          <mi>e</mi>
                          <mi>j</mi>
                        </msub>
                        <mo>&minus;</mo>
                        <mover>
                          <mi>e</mi>
                          <mo>&macr;</mo>
                        </mover>
                        <mo>)</mo>
                      </mrow>
                      <mn>2</mn>
                    </msup>
                  </mrow>
                </msqrt>
                <mspace width="1.6em"/>
                <mtext>不稳定度</mtext>
                <mo>=</mo>
                <mtext>clip</mtext>
                <mo>(</mo>
                <mfrac>
                  <mtext>RMSE</mtext>
                  <mrow>
                    <mn>0.25</mn>
                    <mo>&sdot;</mo>
                    <mi>&tau;</mi>
                  </mrow>
                </mfrac>
                <mo>,</mo>
                <mn>0</mn>
                <mo>,</mo>
                <mn>1</mn>
                <mo>)</mo>
              </mrow>
            </math>
          </div>

          <p class="algo-prose">
            其中 <var>e</var><sub>j</sub> 代表第 <var>j</var> 个落入脉冲梳窗口（<span class="algo-code-inline">&plusmn;comb_tol</span>）内的真实起音峰相对理想网格点的毫秒偏差，<math display="inline"><mover><mi>e</mi><mo>&macr;</mo></mover></math><span class="algo-inline-fallback">e&#772;</span> 代表消除乐器固定起振滞后的一阶均值（分段错相重锁开启时按稳态段独立中心化），<var>m</var> 代表有效命中拍数，<var>&tau;</var> 代表单拍毫秒周期。同样地，当开启变速边界裁剪时，被判为中途变速的区间已从统计拍数中扣除，因此该模式的<strong>强制模式不稳定度</strong>反映的也是稳态主干部分的贴网紧密度。
          </p>
        </div>

        <details class="algo-hardcore-fold">
          <summary><span>底层数学推导：柯西-施瓦茨残差等价性、IEEE-754 防下溢与缩放差异对照</span></summary>
          <div class="algo-fold-content">
            <p>
              <strong>1. 为什么根号里是 <span class="algo-code-inline">(S_kk &middot; S_tt / S_kt&sup2; - 1)</span>？</strong><br>
              设过原点线性模型为 <span class="algo-code-inline">t<sub>i</sub> = k<sub>i</sub><math display="inline"><mover><mi>&tau;</mi><mo>&circ;</mo></mover></math><span class="algo-inline-fallback">&tau;&#770;</span> + r<sub>i</sub></span>，最小二乘解 <span class="algo-code-inline"><math display="inline"><mover><mi>&tau;</mi><mo>&circ;</mo></mover></math><span class="algo-inline-fallback">&tau;&#770;</span> = S<sub>kt</sub> / S<sub>kk</sub></span> 的时域残差平方和为 <span class="algo-code-inline">&sum;r<sub>i</sub>&sup2; = S<sub>tt</sub> - S<sub>kt</sub>&sup2; / S<sub>kk</sub></span>。将其除以基准尺度 <span class="algo-code-inline"><math display="inline"><mover><mi>&tau;</mi><mo>&circ;</mo></mover></math><span class="algo-inline-fallback">&tau;&#770;</span>&sup2; &middot; S<sub>kk</sub> = S<sub>kt</sub>&sup2; / S<sub>kk</sub></span>，即恒等变形为无量纲相对方差项 <span class="algo-code-inline">(S<sub>kk</sub>S<sub>tt</sub> / S<sub>kt</sub>&sup2; - 1)</span>；再乘以无偏自由度修正因子 <span class="algo-code-inline">n / (n - 1)</span> 开方得到无量纲相对不稳定度 <span class="algo-code-inline">rel_inst</span>，最后乘上 <span class="algo-code-inline">BPM</span> 映射回直观的速度波动尺度。
            </p>
            <p>
              <strong>2. 浮点精度顺序与柯西-施瓦茨负零下溢防护：</strong><br>
              在 <span class="algo-code-inline">c_dsp.c</span>（对齐原版汇编 <span class="algo-code-inline">divsd</span> 序列）与 <span class="algo-code-inline">enhanced_v3.py</span> 中，严格执行先乘后两次连除 <span class="algo-code-inline">((S_kk * S_tt) / S_kt) / S_kt - 1.0</span>（不可合并为除以 <span class="algo-code-inline">S_kt * S_kt</span> 以免长曲大数平方溢出或末位舍入偏离）；当极纯电子鼓点完全锁死整数毫秒网格使 <span class="algo-code-inline">S_kk * S_tt == S_kt&sup2;</span> 时，双精度浮点舍入可能出现 <span class="algo-code-inline">-1e-16</span> 微小负数，增强模式内置 <span class="algo-code-inline">max(0.0, term)</span> 守卫以防 <span class="algo-code-inline">sqrt(NaN)</span> 崩溃。同时内部导出时域标准差 <span class="algo-code-inline">&sigma;<sub>t</sub> = &radic;((S<sub>tt</sub> - S<sub>kt</sub>&sup2;/S<sub>kk</sub>) / (n - 1))</span> 供门控判别。
            </p>
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>对比维度</th>
                    <th>Malody模式 (exact)</th>
                    <th>增强模式 (enhanced_v3)</th>
                    <th>强制线性回归模式 (force_v3)</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>取值范围与失败兜底</td>
                    <td>代数域 <span class="algo-code-inline">[0, +&infin;)</span>，实测有效值极难达 <span class="algo-code-inline">0.1</span>（常态 <span class="algo-code-inline">0.000～0.050</span>）；失败返回 <span class="algo-code-inline">0.000</span></td>
                    <td>代数域 <span class="algo-code-inline">[0, +&infin;)</span>，实测有效值极难达 <span class="algo-code-inline">0.1</span>（常态 <span class="algo-code-inline">0.000～0.050</span>）；失败返回 <span class="algo-code-inline">1.000</span></td>
                    <td>截断归一化至 <span class="algo-code-inline">[0.000, 1.000]</span>，失败返回 <span class="algo-code-inline">1.000</span></td>
                  </tr>
                  <tr>
                    <td>倍频折叠与量化缩放</td>
                    <td>随 <span class="algo-code-inline">octave_norm</span> 同步 <span class="algo-code-inline">&times;2 / &divide;2</span>；7 级阶梯吸附改变最终 BPM 时<strong>不重缩放</strong>原残差</td>
                    <td>严格按最终折叠后 <span class="algo-code-inline">final_bpm &times; rel_inst</span> 等比同步缩放</td>
                    <td>按折叠后目标单拍周期 <span class="algo-code-inline">&tau; = 60000 / final_bpm</span> 直接重算 <span class="algo-code-inline">RMSE / (0.25&tau;)</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </details>
      </div>

      <!-- ==================== Tab 5: 易忽略局限：古典与钢琴曲 ==================== -->
      <div class="algo-tab-panel" data-algo-panel="limit" role="tabpanel">
        <div class="algo-quote-card red-accent">
          <div class="algo-quote-lead">
            “一个极易被忽略的算法禁区：本程序对古典乐、类古典/浪漫派轻音乐（如理查德·克莱德曼的钢琴曲、肖邦夜曲、纯弦乐慢板等）在常规模式下基本无解，只能在一定程度上依赖「强制线性回归模式」给出粗略参考。”
          </div>
          <p class="algo-prose">
            许多用户直觉上认为：“只有像《Rush E》那样疯狂加速的曲子才测不出 BPM，一首听起来舒缓平稳的钢琴曲怎么会算不出来？”事实上，<strong>自相关过原点最小二乘（OLS）线性回归得以成立的前提，是曲目具备足够密集的周期性瞬态冲击（足够高的节拍/重音频率）与清晰的宽频打击包络</strong>。而古典乐与理查德式浪漫钢琴曲除了存在演奏者自由呼吸带来的<strong>速度波动（Rubato）较大</strong>外，更致命的症结在于<strong>“双重频率太低”直接击穿了自相关回归的物理下限</strong>。
          </p>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>一、为什么常规自相关回归会失效？——「双重频率太低」与速度波动的叠加崩塌</span>
            <span class="algo-badge red">回归断链根源诊断</span>
          </div>
          <p class="algo-prose">
            在 <strong>Malody模式 (`exact`)</strong> 与 <strong>增强模式 (`enhanced_v3`)</strong> 中，后端必须先在自相关（ACF）序列中找到候选周期峰，再以 <span class="algo-code-inline">&plusmn;8 ms</span>（可调 <span class="algo-code-inline">5～25 ms</span>）窗口沿整数倍谐波链 <span class="algo-code-inline">1&tau;, 2&tau;, &hellip;, k&tau;</span> 逐拍递推回归。面对古典与钢琴曲时，以下三大机理会同时锁死回归管线：
          </p>

          <div class="algo-math-box" aria-label="低频长周期下绝对相移放大与窗口样本枯竭公式">
            <div class="algo-math-badge">低频长周期窗口样本枯竭与绝对相移放大定律</div>
            {_MATHML_FALLBACK_BOX_HTML}
            <math display="block">
              <mrow>
                <msub>
                  <mi>K</mi>
                  <mtext>max</mtext>
                </msub>
                <mo>=</mo>
                <mo>&lfloor;</mo>
                <mfrac>
                  <msub>
                    <mi>L</mi>
                    <mtext>win</mtext>
                  </msub>
                  <msub>
                    <mi>&tau;</mi>
                    <mtext>eff</mtext>
                  </msub>
                </mfrac>
                <mo>&rfloor;</mo>
                <mspace width="1.6em"/>
                <mi>&Delta;</mi>
                <mi>t</mi>
                <mo>=</mo>
                <msub>
                  <mi>&delta;</mi>
                  <mtext>rubato</mtext>
                </msub>
                <mo>&sdot;</mo>
                <msub>
                  <mi>&tau;</mi>
                  <mtext>eff</mtext>
                </msub>
                <mo>=</mo>
                <mfrac>
                  <mrow>
                    <mn>60000</mn>
                    <mo>&sdot;</mo>
                    <msub>
                      <mi>&delta;</mi>
                      <mtext>rubato</mtext>
                    </msub>
                  </mrow>
                  <msub>
                    <mi>f</mi>
                    <mtext>beat</mtext>
                  </msub>
                </mfrac>
                <mo class="algo-op-highlight">&gg;</mo>
                <mo class="algo-op-highlight">&plusmn;</mo>
                <mn class="algo-op-highlight" style="color:#ff7b72;">8</mn>
                <mspace width="0.25em"/>
                <mtext style="color:#ff7b72;">ms</mtext>
              </mrow>
            </math>
          </div>

          <ul class="algo-list">
            <li>
              <strong>1. 节拍/重音出现频率太低（有效脉冲频率 <var>f</var><sub>beat</sub> 过低）&rarr; 窗口样本枯竭与长周期绝对相移暴涨：</strong><br>
              流行与电音曲目有“每拍必响”的底鼓（Four-on-the-floor），每秒钟提供 <span class="algo-code-inline">2～3 次</span> 强瞬态锚点（周期 <span class="algo-code-inline">&tau; &approx; 300～500 ms</span>）；而抒情钢琴曲与古典慢板一方面本身拍频（BPM）偏慢，另一方面缺乏每拍打击乐，往往<strong>每小节（甚至每两小节）的强拍才有一记明显的左手低音八度下键</strong>，中间全是轻柔的连奏旋律或分解琶音。这使得主干重音的有效触发频率低至 <span class="algo-code-inline">0.4～0.8 Hz</span>（等效周期 <span class="algo-code-inline">&tau;<sub>eff</sub></span> 长达 <span class="algo-code-inline">1200～2500 ms</span>）：<br>
              &bull; <strong>样本量枯竭：</strong>后端初筛候选窗默认仅扫描前 <span class="algo-code-inline">4000 ms</span>（池化窗 <span class="algo-code-inline">8000 ms</span>），当 <span class="algo-code-inline">&tau;<sub>eff</sub> &gt; 1200 ms</span> 时，整个窗口内最多只能容纳 <span class="algo-code-inline">1～3 个</span> 谐波周期，直接跌破 Malody模式 <span class="algo-code-inline">n &ge; 4</span>（增强模式 <span class="algo-code-inline">n &ge; 3</span>）的最低成链门槛而报 <span class="algo-code-inline">0.000 BPM</span>！<br>
              &bull; <strong>长周期误差放大：</strong>由上式可知，相同的相对速度波动率 <span class="algo-code-inline">&delta;<sub>rubato</sub></span> 乘上超长周期 <span class="algo-code-inline">&tau;<sub>eff</sub></span> 后，绝对毫秒偏差会成倍放大。例如 <span class="algo-code-inline">180 BPM</span>（<span class="algo-code-inline">333 ms</span>）下 <span class="algo-code-inline">2%</span> 的微颤仅偏移 <span class="algo-code-inline">6.7 ms</span>（安全落在 <span class="algo-code-inline">&plusmn;8 ms</span> 回归窗内）；而在等效重音周期 <span class="algo-code-inline">1500 ms</span> 的慢速钢琴曲中，哪怕只有 <span class="algo-code-inline">2%</span> 的呼吸起伏，单拍绝对相移就会高达 <strong>30 ms</strong>——远远甩出 <span class="algo-code-inline">&plusmn;8 ms</span> 寻峰窗口，导致递推回归在最初两三拍就彻底断锁！
            </li>
            <li>
              <strong>2. 声学频谱频率（Hz）偏低且缺乏高频打击瞬态 &rarr; 滤波器组失配与延音踏板糊化包络：</strong><br>
              后端前级 4 频段双二阶滤波器组（<span class="algo-code-inline">filters.py</span>）专为捕捉打击乐设计，将 <strong>50% 的总权重</strong>押注在军鼓中高频带通（Band 2：<span class="algo-code-inline">1200～2400 Hz</span>，权重 <span class="algo-code-inline">0.20</span>）与镲片高通（Band 3：<span class="algo-code-inline">&gt;2400 Hz</span>，权重 <span class="algo-code-inline">0.30</span>），另有 <span class="algo-code-inline">0.30</span> 权重位于底鼓低通（Band 0：<span class="algo-code-inline">&lt;600 Hz</span>，附带 <span class="algo-code-inline">-320</span> 采样点大群延迟平滑）。然而钢琴（基频主能量在 <span class="algo-code-inline">80～1000 Hz</span>）与大提琴/圆号/弦乐群的声学频率偏低，<strong>既无架子鼓在 &gt;2400 Hz 的陡峭宽频金属瞬态，又在低频区受制于弦乐慢起音（Slow Attack）与钢琴延音踏板（Sustain Pedal）的共振尾音交叠</strong>。结果生成的 <span class="algo-code-inline">1000 Hz</span> 差分包络呈现圆钝起伏的波浪而非尖锐脉冲，自相关峰顶宽平模糊，极易在二次抛物线插值与回归时发生漂移。
            </li>
            <li>
              <strong>3. 浪漫派弹性速度（Rubato）与左手流动琶音的“喧宾夺主”：</strong><br>
              以理查德·克莱德曼（如《梦中的婚礼》《秋日私语》）及古典钢琴曲为代表的作品，演奏灵魂在于“句头微催、句尾渐慢（Ritardando）、换气延留（Fermata）”，全曲根本不存在刚性恒定的物理周期；同时左手连绵不断的八分/十六分分解琶音（Arpeggio）在时域上的敲击密度远高于小节强拍，容易在短滞后区制造密集的琶音伪谐波峰，误导常规算法锁入错误细分倍率。
            </li>
          </ul>
        </div>

        <div class="algo-section-card">
          <div class="algo-section-title">
            <span>二、为什么只能「一定程度上依赖强制线性回归模式 (force_v3)」？</span>
            <span class="algo-badge purple">兜底机理与客观天花板</span>
          </div>
          <p class="algo-prose">
            当常规模式因上述“低频稀疏 + 踏板糊化 + 呼吸波动”报 <span class="algo-code-inline">0.000 BPM</span> 或严重跑偏时，切换至<strong>强制线性回归模式 (`force_v3`)</strong> 是唯一能提取出有效参考值的手段，但使用者必须清楚其<strong>能做什么、不能做什么</strong>：
          </p>

          <div class="algo-mode-grid">
            <div class="algo-mode-card force">
              <div class="algo-section-title">
                <span class="algo-badge purple">为什么强制模式能算出结果？（无门槛全局谐波梳积分）</span>
              </div>
              <p class="algo-prose">
                <!-- [修改 2026-11-15] 精确同步 V3 周期区间 -->
                强制模式（<span class="algo-code-inline">force_v3.py :: compute_comb_projection_spectrum</span>）<strong>彻底废除了“必须连续命中 3～4 拍且每拍偏差 &le; &plusmn;8 ms”的逐拍递推门限</strong>，也不要求自相关峰必须具备陡峭的局部极大值。它在候选周期 <span class="algo-code-inline">&tau; &isin; [199.99, 1000.17] ms</span>（<span class="algo-code-inline">59.99～300.01 BPM</span>）上，直接向长达 <strong>30,000 ms（30 秒）</strong>的全局自相关曲线发射等距谐波梳 <span class="algo-code-inline">k&tau;</span> 进行全链线性插值能量积分。哪怕钢琴包络峰顶圆钝、低音重拍每隔一两秒才出现一次、甚至中途有几拍因延音踏板完全缺失，只要全曲在某个平均拍频（或其琶音倍频）上存在微弱的统计共振，梳状投影就能把这股分散的能量聚拢起来，锁定<strong>全曲相干能量绝对值最高的“统计平均周期”</strong>。
              </p>
            </div>

            <div class="algo-mode-card">
              <div class="algo-section-title">
                <span class="algo-badge amber">为什么说“只能一定程度上依赖”？（不可逾越的物理天花板）</span>
              </div>
              <p class="algo-prose">
                <strong>1. 统计平均重心 &ne; 全曲刚性对音网格：</strong>强制模式给出的本质上是把整首弹性速度钢琴曲“强行拉平”后的单一折中斜率。对于充满呼吸伸缩的古典/理查德钢琴曲，这个数字能帮你快速获知“全曲中枢大约在多少 BPM 附近”，但若直接将该单一数值填入写谱器铺满全曲，往往写过两三个乐句就会因原曲渐慢/抢拍而整体错开。<br>
                <strong>2. 分解琶音倍频与三拍子吸附偏向：</strong>由于主拍低音频率过低，强制模式的能量梳往往更容易捕捉到能量更密集的左手伴奏琶音周期（如将慢速 <span class="algo-code-inline">68 BPM</span> 四分音符吸附到三连音/八分琶音对应的 <span class="algo-code-inline">102 / 136 / 204 BPM</span> 同源倍频上），制谱时仍需人工结合听感做 <span class="algo-code-inline">&divide;2 / &divide;1.5</span> 倍率核对或手动分小节对音。
              </p>
            </div>
          </div>
        </div>

        <details class="algo-hardcore-fold">
          <summary><span>不同曲风在后端 4 频段包络、自相关谐波密度与三模式适用性上的完整对照表</span></summary>
          <div class="algo-fold-content">
            <div class="algo-table-wrap">
              <table class="algo-spec-table">
                <thead>
                  <tr>
                    <th>曲风类型</th>
                    <th>声学频段与瞬态特征</th>
                    <th>有效重拍频率与 ACF 谐波密度</th>
                    <th>三模式实测表现与制谱建议</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>现代电音 / 流行 / 摇滚 / 音游曲</td>
                    <td>底鼓（&lt;600 Hz）与军鼓镲片（&gt;1200/2400 Hz）瞬态极强，完美契合 4 频段权重分配</td>
                    <td>每秒 2～4 拍高频触发，4s/8s 窗内含 8～30+ 阶清晰谐波尖峰，单拍抖动 &lt; 2 ms</td>
                    <td><strong>增强模式主力通吃</strong>（精度达 <span class="algo-code-inline">0.001 BPM</span>，可直接单 BPM 铺满稳态主干）</td>
                  </tr>
                  <tr>
                    <td>类古典轻音乐 / 浪漫钢琴（如理查德钢琴曲）</td>
                    <td>能量集中在中低频琴弦基音，缺 &gt;2400 Hz 打击瞬态，延音踏板使差分包络峰形宽平</td>
                    <td>重音稀疏（常按小节触发，周期 &gt;1000 ms），4s 窗内有效谐波 &le;3 阶，且伴随句尾渐慢</td>
                    <td>常规模式易断链报 <span class="algo-code-inline">0.000</span> 或锁偏；<strong>需切至强制线性回归模式提取中枢参考速度</strong>，并配合人工核对琶音倍率与乐句变速</td>
                  </tr>
                  <tr>
                    <td>纯古典独奏 / 交响慢板 / 自由散板</td>
                    <td>管弦乐慢起音（Slow Attack）长达数十毫秒，无任何打击乐瞬态前沿，包络信噪比极低</td>
                    <td>拍频极低且全程自由变速（Rubato），自相关远端谐波完全散焦平坦，无周期相干峰</td>
                    <td>物理上不存在单一恒定网格；强制模式仅能给出局部统计均值，制谱必须依赖人工逐小节打轴（SV 变速）</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </details>
      </div>
    </div>

    <div class="modal-footer" style="justify-content:flex-end;">
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-filled" id="doneAlgoDocModalBtn">我知道了</button>
      </div>
    </div>
  </div>
</div>
"""

_NO_MULTIBPM_MODAL_HTML = f"""
<!-- 关于程序不提供多BPM的说明独立模态弹窗 (web_gui_algo_doc.py) -->
<div class="modal-backdrop" id="noMultiBpmModal" role="dialog" aria-modal="true" aria-labelledby="noMultiBpmModalTitle">
  <div class="modal-dialog no-multibpm-dialog">
    <div class="modal-header">
      <h2 id="noMultiBpmModalTitle">关于程序不提供多BPM的说明</h2>
      <button type="button" class="modal-close-btn" id="closeNoMultiBpmModalBtn" title="关闭">✕</button>
    </div>
    <div class="algo-doc-header-sub">
      <div style="display:inline-flex; align-items:center; gap:8px; flex-wrap:wrap;">
        <span class="algo-badge amber">工程取舍与制谱现实</span>
        <span style="font-size:13.5px; color:#c9d1d9;">宁可把一个稳态主干算到亚毫秒级极致，也不输出一堆毫无制谱价值的碎片数字</span>
      </div>
    </div>

    <!-- 首页不提供多BPM说明弹窗：顶部章节目录与锚点快速导航 (web_gui_algo_doc.py) -->
    <div class="no-multibpm-toc-bar" aria-label="文档目录快速导航">
      <span class="no-multibpm-toc-label">📑 目录导航:</span>
      <button type="button" class="no-multibpm-toc-pill" data-no-multi-jump="sec1">一、局部短变速</button>
      <button type="button" class="no-multibpm-toc-pill" data-no-multi-jump="sec2">二、进阶四反问</button>
      <button type="button" class="no-multibpm-toc-pill" data-no-multi-jump="sec3">三、连续变速与散板</button>
      <button type="button" class="no-multibpm-toc-pill" data-no-multi-jump="sec4">四、Rush E与整活曲</button>
      <button type="button" class="no-multibpm-toc-pill" data-no-multi-jump="sec5">五、练琴与古典乐诉求</button>
      <button type="button" class="no-multibpm-toc-pill proof-pill" data-no-multi-jump="secFold">六、底层数学证明</button>
    </div>

    <div class="modal-body algo-doc-body" id="noMultiBpmScrollBody">
      {_MATHML_TOP_BANNER_HTML}
      <div class="algo-quote-card amber-accent" id="noMultiSec1">
        <div class="algo-section-title">
          <span>一、局部短变速：「顾此必失彼」，强算不仅无用还会搞崩主干</span>
          <span class="algo-badge amber">制谱性价比铁律</span>
        </div>
        <div class="algo-quote-lead">
          “顾此必失彼——常规曲目里的变速部分占主干往往很低，本身没有独立计算的价值；强行切段计算不但误差极大、根本无法用于制谱，还会把稳态主干那头也一并搞崩，纯属得不偿失。”
        </div>
        <p class="algo-prose">
          在音游写谱器的刚性网格下，<strong>BPM 的可用性高度依赖回归基线长度</strong>：一段持续 2～3 分钟的稳态主干拥有数百拍跨度，本程序能将单拍斜率死死钉在 <span class="algo-code-inline">0.001 BPM</span> 量级；而曲目中途几秒钟（往往只有 4～8 拍）的过门加减速或桥段缓冲，受限于物理样本数极少，哪怕真实乐器起音只有几毫秒的自然微颤，硬算出来的局部速度也会轻易偏出 <span class="algo-code-inline">&plusmn;0.5～1.5 BPM</span>——填进写谱器里连几小节都对齐不了。更致命的是，如果为了迁就并捕捉这些短促变速而缩窄自相关分析窗或放宽容差门限，原本稳如磐石的主干长程回归就会被切碎污染。因此本程序采取最务实的策略：<strong>在结果卡片「变速区域」中标出起止位置供谱师定位参考，计算上则将其干净剥离，集中全部算力保住主干（含 a tempo 恢复原速段）的绝对精度。</strong>
        </p>
      </div>

      <div class="algo-quote-card" id="noMultiSec2">
        <div class="algo-section-title">
          <span>二、进阶追问：变速段占比或时长较大为什么也不自动测？</span>
          <span class="algo-badge blue">灵魂四问 &middot; 变点与采样点困局</span>
        </div>
        <div class="algo-quote-lead">
          “有人可能会问了：如果曲子变速段占比或时长比较大，那应该可以测啊！”——那么不妨先直面下面这四个最现实的工程与制谱追问：”
        </div>
        <div class="algo-rebuttal-list">
          <div class="algo-rebuttal-item">
            <div class="algo-rebuttal-head">
              <span class="algo-rebuttal-idx">反问 1</span>
              <span>您认为把这个「变速占比阈值」设为多少合适？</span>
            </div>
            <div class="algo-rebuttal-ans">
              设 15%、25% 还是 35%？任何硬编码比例都是一刀切的人为悬崖：阈值调低一点，常规单速曲里的抽鼓人声桥段、Half-time 半速听感编曲或重切分过门就会被误判成“独立新速度段”，反过来把本该稳如泰山的纯单 BPM 曲切碎搞崩；阈值调高一点，真正非对称的双速曲照样卡在线下漏判。编曲织体变化与真实变速占比之间，物理上根本不存在一条放之四海皆准的百分比分界线。
            </div>
          </div>

          <div class="algo-rebuttal-item">
            <div class="algo-rebuttal-head">
              <span class="algo-rebuttal-idx">反问 2</span>
              <span>如果真的达到可以独立分析的程度了，为什么不可以手动剪出来单独分析？</span>
            </div>
            <div class="algo-rebuttal-ans">
              如果某一段异速部分已经稳态持续了几十秒、有效拍数充沛到足以压住短基线方差，谱师在任意音频工具里花 10 秒钟把它干净裁出来、单独丢进本程序（必要时配合设置里的时长解锁开关），立刻就能拿到<strong>零跨段串扰、零交界污染的 <span class="algo-code-inline">0.001 BPM</span> 级纯粹主干解</strong>——放着零污染的手工切片单测不用，何必指望黑盒自动切分器在模糊交界处带着混叠污染去硬猜？
            </div>
          </div>

          <div class="algo-rebuttal-item">
            <div class="algo-rebuttal-head">
              <span class="algo-rebuttal-idx">反问 3</span>
              <span>假设是 <span class="algo-code-inline">180 &rarr; 181</span>，哪怕全曲是“精准半开”，有什么办法可以精准确定哪里脱速了？</span>
            </div>
            <div class="algo-rebuttal-ans">
              算一笔最直观的声学账：<span class="algo-code-inline">180 BPM</span>（<span class="algo-code-inline">333.333 ms/拍</span>）与 <span class="algo-code-inline">181 BPM</span>（<span class="algo-code-inline">331.492 ms/拍</span>）单拍周期仅仅相差 <strong><span class="algo-code-inline">1.842 ms</span>（44.1 kHz 下仅约 81 个采样点）</strong>！而真实鼓点与乐器起音包络的自然微抖动本身就高达 <span class="algo-code-inline">&plusmn;5～15 ms</span>（是单拍速度差的 3～8 倍，更远小于 <span class="algo-code-inline">&plusmn;35 ms</span> 相位梳状容差窗）。相位漂移必须连续攒够十余拍（好几秒钟）才能勉强浮出起音微颤的噪声海面——哪怕前后严格 50% 精准半开，交界处照样横亘着数秒宽的<strong>物理变点盲区</strong>，没有任何算法能凭空指认具体从哪一拍、哪一毫秒开始脱速。
            </div>
          </div>

          <div class="algo-rebuttal-item">
            <div class="algo-rebuttal-head">
              <span class="algo-rebuttal-idx">反问 4</span>
              <span>最简单的 <span class="algo-code-inline">BAB</span> 式结构（头尾同速、中间另一个速），给了两个 BPM 也大概率确定不了中间具体哪个采样点偏了。</span>
            </div>
            <div class="algo-rebuttal-ans">
              在音游写谱器里，变速轴<strong>光有速度数字毫无意义。每一根变速红线都必须绑定毫秒/采样点级的绝对相位锚点 <span class="algo-code-inline">(Offset, BPM)</span></strong>。以最简单的 <span class="algo-code-inline">BAB</span> 三明治曲式为例：由于中间异速 <span class="algo-code-inline">A</span> 段的物理时长几乎不可能恰好凑成头尾 <span class="algo-code-inline">B</span> 段拍长的严格整数倍，尾部恢复 <span class="algo-code-inline">B</span> 速时相对头部网格必然产生任意分拍相位偏移。就算程序把两个 BPM 数字都算给你，它也定不出 <span class="algo-code-inline">B&rarr;A</span> 具体从哪个采样点偏出去、<span class="algo-code-inline">A&rarr;B</span> 又在哪个采样点切回来——谱师进写谱器后，照样得放大波形包络逐采样点手钉两处变点红线。（顺带一提：对于占主导的头尾同速 <span class="algo-code-inline">B</span> 本身，本程序现有的<strong>多主干联合回归</strong>早已能通过分段独立截距 <var>b</var><sub>1</sub>, <var>b</var><sub>2</sub> 跨过中间 <span class="algo-code-inline">A</span> 段直接联立锁死主干速度，根本无需依赖中间段的精确采样点边界。）
            </div>
          </div>
        </div>
      </div>

      <div class="algo-section-card" id="noMultiSec3">
        <div class="algo-section-title">
          <span>三、连续渐变与自由散板：当稳态台阶都不存在，离散「测 BPM」本身已是伪命题</span>
          <span class="algo-badge blue">物理定义边界</span>
        </div>
        <p class="algo-prose">
          <strong>一旦全曲铺满大面积连续变速或自由散板，这便早已不再是“变不变速、算一个还是算几个 BPM”的技术问题，客观上也就没有机器硬算的必要了。</strong>
        </p>
        <p class="algo-prose">
          无论是全曲频繁自由呼吸的交响散板（Rubato），还是大段铺满连续 <span class="algo-code-inline">accel.</span>（渐快）与 <span class="algo-code-inline">rit.</span>（渐慢）的曲目，其瞬时速度 <var>v</var>(<var>t</var>) 在物理上是一条连续滑动的曲线，而非若干段水平常数台阶。对于连稳态等距网格都不存在的段落，任何自动切分吐出的离散数字都只是对连续斜坡的粗糙阶梯近似；在真实制谱流程中，这类曲目唯有依靠谱师对照音频波形与起峰包络逐小节手工拉轴对齐，强行提供“多段自动 BPM”除了制造虚假精确的幻觉外别无益处。
        </p>
      </div>

      <div class="algo-quote-card" id="noMultiSec4">
        <div class="algo-section-title">
          <span>四、从《Rush E》与正弦变速整活曲看算法的「有所不为」</span>
          <span class="algo-badge purple">工程定力与路线自信</span>
        </div>
        <div class="algo-quote-lead">
          “现有这套算法体系在正常曲库里几乎无往不利，真正撞墙的大概只有《Rush E》——但面对这种从头到尾塞进 10 多个短促速度档位、期间还层层叠满 accel. 与 rit. 的纯折磨向曲目，就算程序真给你算出 10 多个 BPM，你就敢把它们一个个填进写谱器去吗？”
        </div>
        <p class="algo-prose">
          没有精确到拍的变速锚点与过渡斜率，手里攥着十几个短命的毛刺 BPM 进写谱器只会是一场灾难。至于那些刻意用<strong>正弦函数连续扭曲速度（正弦变速，即BPM随时间轴按正弦函数不停变化）</strong>专门拿来折磨玩家与谱师的整活曲，本质上就只适合留在猎奇整活的小圈子里自成一派，本就不属于严肃音频测速应当迎合的范畴。本程序的底层多频段瞬态提纯与多主干联合回归体系已经具备足够扎实的算法含金量。真正成熟的工程设计深知边界所在——<strong>程序越是不向这类极端整活曲妥协，就越能证明“剥离瞬态噪声、死磕稳态主干”这条路走对了。</strong>
        </p>
      </div>

      <!-- 第五章节：针对练琴与古典乐鉴赏诉求的说明 (web_gui_algo_doc.py) -->
      <div class="algo-quote-card red-accent" id="noMultiSec5">
        <div class="algo-section-title">
          <span>五、练琴与古典乐鉴赏诉求：与音游写谱刚性网格背道而驰，本程序绝不迎合</span>
          <span class="algo-badge red">核心服务宗旨 &middot; 拒绝盲从</span>
        </div>
        <div class="algo-quote-lead">
          “「我是练琴的 / 我是古典乐鉴赏家，我这里有首多 BPM 的曲子，我想用这个程序把每段速度都算出来。」——那么非常抱歉，这与本程序的核心服务宗旨严重背道而驰。以练琴与鉴赏的实际需求而言，您完全可以自己手动打拍测算，多一点、少一点根本无关痛痒，完全不应依赖本程序。”
        </div>
        <p class="algo-prose">
          本程序倾注全部精力攻坚的多频段瞬态提纯与亚毫秒自相关回归，从立项第一天起就是<strong>专门为音游（节奏动作游戏）写谱器的毫秒级绝对刚性网格而生</strong>的。在音游制谱现实中，速度差出 <span class="algo-code-inline">0.1 BPM</span> 就意味着全曲过半后音符与重拍彻底错位脱节，谱师需要的是锁死到小数点后三位的绝对基准；而古典乐、钢琴独奏的艺术灵魂恰恰在于演奏者赋予曲目的弹性呼吸（Rubato）与情感起伏——它在物理声学上根本不存在刚性等距网格，强行套用离散数字本身就是对音乐艺术性的割裂。
        </p>
        <p class="algo-prose">
          退一万步讲，无论练习指法还是欣赏乐曲结构，您只需要宏观感知这一乐段大致在 70 还是 120 速度区间；即便随手用节拍器打点估算出 <span class="algo-code-inline">72</span> 还是 <span class="algo-code-inline">74</span>，对练琴与鉴赏而言根本无关痛痒。对于这种“容差极大、根本不需要高精锁相”的宽泛诉求，完全可以由人工手动解决。如果本程序为了迎合这种外部需求而拼凑出一堆似是而非的所谓“分段 BPM”，既是对精密算法严肃性的自我否定，更会对真正需要严格相位参考的音游谱师造成灾难级的误导。<strong>分清谁是自己的核心服务对象，有所为而有所不为，正是本程序始终坚守的工程底线。</strong>
        </p>
      </div>

      <!-- 第六章节：底层硬核数理证明（直出展示，彻底杜绝折叠手风琴在特定浏览器下高度塌缩为单条白线问题） (web_gui_algo_doc.py) -->
      <div class="algo-section-card" id="noMultiSecFold">
        <div class="algo-section-title">
          <span>六、底层数学证明：短基线误差 N<sup>-3/2</sup> 放大定律与微幅变点物理盲区</span>
          <span class="algo-badge amber">底层硬核规格 &middot; 刚性铁证</span>
        </div>
        <div class="algo-fold-content" style="padding:4px 0 0 0;">
          <div class="algo-fold-subhead">
            <span class="algo-badge amber">证明一</span>
            <span>过原点回归误差 N<sup>-3/2</sup> 短基线放大定律与本程序净段剥离门限</span>
          </div>
          <p>
            为什么几秒钟的短变速段在物理上不可能算准？对过原点等距节拍序列 <span class="algo-code-inline"><var>t</var><sub>k</sub> = <var>k</var>&tau; + &epsilon;<sub>k</sub></span>（设单拍起音自然微抖动标准差为 <span class="algo-code-inline">&sigma;<sub>t</sub></span>），最小二乘周期估计量 <span class="algo-code-inline"><math display="inline"><mover><mi>&tau;</mi><mo>&circ;</mo></mover></math><span class="algo-inline-fallback">&tau;&#770;</span> = &sum;<var>k</var><var>t</var><sub>k</sub> / &sum;<var>k</var>&sup2;</span> 的标准误由平方和公式 <span class="algo-code-inline">&sum;<sub>k=1</sub><sup>N</sup> k&sup2; = N(N+1)(2N+1)/6 &approx; N&sup3;/3</span> 严格决定，呈现<strong>随有效拍数 <var>N</var> 的 1.5 次方急剧收敛/发散</strong>的铁律：
          </p>
          <div class="algo-math-box" aria-label="短基线过原点回归标准误放大公式">
            <div class="algo-math-badge">短基线过原点回归误差 N⁻³⸍² 发散定律</div>
            {_MATHML_FALLBACK_BOX_HTML}
            <math display="block">
              <mrow>
                <mtext>SE</mtext>
                <mo>(</mo>
                <mover>
                  <mi>&tau;</mi>
                  <mo>&circ;</mo>
                </mover>
                <mo>)</mo>
                <mo>=</mo>
                <mfrac>
                  <msub>
                    <mi>&sigma;</mi>
                    <mi>t</mi>
                  </msub>
                  <msqrt>
                    <mrow>
                      <munderover>
                        <mo>&sum;</mo>
                        <mrow>
                          <mi>k</mi>
                          <mo>=</mo>
                          <mn>1</mn>
                        </mrow>
                        <mi>N</mi>
                      </munderover>
                      <msup>
                        <mi>k</mi>
                        <mn>2</mn>
                      </msup>
                    </mrow>
                  </msqrt>
                </mfrac>
                <mo>&approx;</mo>
                <mfrac>
                  <mrow>
                    <msqrt>
                      <mn>3</mn>
                    </msqrt>
                    <mo>&sdot;</mo>
                    <msub>
                      <mi>&sigma;</mi>
                      <mi>t</mi>
                    </msub>
                  </mrow>
                  <msup>
                    <mi>N</mi>
                    <mrow>
                      <mn>3</mn>
                      <mo>/</mo>
                      <mn>2</mn>
                    </mrow>
                  </msup>
                </mfrac>
                <mspace width="1.6em"/>
                <msub>
                  <mi>&sigma;</mi>
                  <mtext>BPM</mtext>
                </msub>
                <mo>&approx;</mo>
                <mfrac>
                  <msup>
                    <mtext>BPM</mtext>
                    <mn>2</mn>
                  </msup>
                  <mn>60000</mn>
                </mfrac>
                <mo>&sdot;</mo>
                <mfrac>
                  <mrow>
                    <msqrt>
                      <mn>3</mn>
                    </msqrt>
                    <mo>&sdot;</mo>
                    <msub>
                      <mi>&sigma;</mi>
                      <mi>t</mi>
                    </msub>
                  </mrow>
                  <msup>
                    <mi>N</mi>
                    <mrow>
                      <mn>3</mn>
                      <mo>/</mo>
                      <mn>2</mn>
                    </mrow>
                  </msup>
                </mfrac>
              </mrow>
            </math>
          </div>
          <div class="algo-table-wrap">
            <table class="algo-spec-table">
              <thead>
                <tr>
                  <th>段落类型（以 150 BPM、起音微颤 &sigma;<sub>t</sub> = 6 ms 为例）</th>
                  <th>有效拍数 N（时长）</th>
                  <th>N<sup>3/2</sup> 基线因子</th>
                  <th>BPM 理论标准误 (&pm;1&sigma;)</th>
                  <th>本程序源码实装处理策略</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>全曲稳态主干</td>
                  <td><span class="algo-code-inline">360 拍</span>（144 秒）</td>
                  <td><span class="algo-code-inline">6,830.5</span></td>
                  <td><strong>&pm;0.0006 BPM</strong>（锁死千分位）</td>
                  <td>全量纳入长程 / 多主干联合 OLS 回归</td>
                </tr>
                <tr>
                  <td>增强模式二次精修最低主净段门槛</td>
                  <td><span class="algo-code-inline">&ge;41 拍</span>（<strong>&ge;16.5 秒</strong>）</td>
                  <td><span class="algo-code-inline">&ge;262.5</span></td>
                  <td>约 &pm;0.015 BPM（副净段默认 &ge;15.0s，可调 8～15s）</td>
                  <td>满足门槛方触发剔除变速二次联合重算，否则保留初测稳健解</td>
                </tr>
                <tr>
                  <td>强制模式二次重搜最低净段门槛</td>
                  <td><span class="algo-code-inline">&ge;20 拍</span>（<strong>&ge;8.0 秒</strong>）</td>
                  <td><span class="algo-code-inline">&ge;89.4</span></td>
                  <td>约 &pm;0.044 BPM（单净段须 &ge;4.0s）</td>
                  <td>合成无跨缝 <span class="algo-code-inline">acf_clean</span> 执行 0.01ms 超细加密投影重搜</td>
                </tr>
                <tr>
                  <td>典型过门短变速 / 《Rush E》碎片档位</td>
                  <td><span class="algo-code-inline">6～8 拍</span>（2.4～3.2 秒）</td>
                  <td><span class="algo-code-inline">14.7～22.6</span></td>
                  <td><span style="color:#ff7b72; font-weight:600;">暴涨约 300～460 倍（95% 区间逾 &pm;0.5～1.0 BPM）</span></td>
                  <td>仅在「变速区域」提示时间戳，严禁独立定速或混入主干</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="algo-fold-divider"></div>

          <div class="algo-fold-subhead">
            <span class="algo-badge blue">证明二</span>
            <span>微幅跳变（以 180 &rarr; 181 BPM 为例）变点检测信噪比极限与采样点相位盲区</span>
          </div>
          <p>
            为什么哪怕全曲“精准半开”或采用最简单的 <span class="algo-code-inline">BAB</span> 结构，也无法精确锁定脱速采样点？设真实速度在未知变点 <span class="algo-code-inline">n<sub>0</sub></span> 处从 <var>B</var><sub>1</sub> 阶跃至 <var>B</var><sub>2</sub>，单拍周期阶跃量仅为 <span class="algo-code-inline">&Delta;&tau; = |&tau;<sub>1</sub> &minus; &tau;<sub>2</sub>|</span>。在单拍真实起音包络存在标准差 <span class="algo-code-inline">&sigma;<sub>t</sub></span> 的自然微抖动背景下，变点后第 <var>m</var> 拍的累积相位偏离 <span class="algo-code-inline">&Delta;&Phi;(m) = m &middot; &Delta;&tau;</span> 至少必须跨过 <span class="algo-code-inline">3&sigma;<sub>t</sub></span> 显著性门限（否则任何正常鼓点微颤都会触发虚假断拍），由此严格锁死了<strong>变点定位的最小不可观测模糊拍数 <var>m</var><sub>min</sub></strong>：
          </p>
          <div class="algo-math-box purple-border" aria-label="微幅速度跳变变点检测信噪比极限公式">
            <div class="algo-math-badge">微幅速度跳变变点检测信噪比极限与盲区拍数</div>
            {_MATHML_FALLBACK_BOX_HTML}
            <math display="block">
              <mrow>
                <mi>&Delta;</mi>
                <mi>&tau;</mi>
                <mo>=</mo>
                <mrow>
                  <mo>|</mo>
                  <mfrac>
                    <mn>60000</mn>
                    <msub>
                      <mi>B</mi>
                      <mn>1</mn>
                    </msub>
                  </mfrac>
                  <mo>&minus;</mo>
                  <mfrac>
                    <mn>60000</mn>
                    <msub>
                      <mi>B</mi>
                      <mn>2</mn>
                    </msub>
                  </mfrac>
                  <mo>|</mo>
                </mrow>
                <mspace width="1.6em"/>
                <msub>
                  <mi>m</mi>
                  <mtext>min</mtext>
                </msub>
                <mo>&ge;</mo>
                <mo>&lceil;</mo>
                <mfrac>
                  <mrow>
                    <mn>3</mn>
                    <mo>&sdot;</mo>
                    <msub>
                      <mi>&sigma;</mi>
                      <mi>t</mi>
                    </msub>
                  </mrow>
                  <mrow>
                    <mi>&Delta;</mi>
                    <mi>&tau;</mi>
                  </mrow>
                </mfrac>
                <mo>&rceil;</mo>
              </mrow>
            </math>
          </div>
          <p>
            代入 <span class="algo-code-inline">180 &rarr; 181 BPM</span> 实算：<span class="algo-code-inline">&tau;<sub>1</sub> = 333.333 ms</span>，<span class="algo-code-inline">&tau;<sub>2</sub> = 331.492 ms</span>，单拍周期差仅 <span class="algo-code-inline">&Delta;&tau; = 1.842 ms</span>（在 <span class="algo-code-inline">44.1 kHz</span> 音频中每拍仅差 <strong>81 个采样点</strong>，<span class="algo-code-inline">48 kHz</span> 下仅差 <strong>88 个采样点</strong>）。取常规混音起音微颤 <span class="algo-code-inline">&sigma;<sub>t</sub> = 6 ms</span>（<span class="algo-code-inline">3&sigma;<sub>t</sub> = 18 ms</span>），则 <span class="algo-code-inline">m<sub>min</sub> = &lceil;18 / 1.842&rceil; = 10 拍</span>——意味着变点附近至少存在 <strong>3.31 秒（逾 14.6 万个采样点）的物理信噪比盲区</strong>！同理在 <span class="algo-code-inline">BAB</span> 结构中，中间 <span class="algo-code-inline">A</span> 段（设长 <var>L</var><sub>A</sub>）给尾部 <span class="algo-code-inline">B</span> 段留下的残余相位偏移为 <span class="algo-code-inline">&delta;&phi; = L<sub>A</sub> mod &tau;<sub>B</sub></span>，切入或切出位置只要漏判几拍，制谱网格就会整体错位——这从数学底层决定了“只给两个孤立 BPM 数字，照样定不出中间具体哪个采样点偏了”。
          </p>
        </div>
      </div>
    </div>

    <div class="modal-footer" style="justify-content:flex-end;">
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-filled" id="doneNoMultiBpmModalBtn">我知道了</button>
      </div>
    </div>
  </div>
</div>
"""

_ALGO_DOC_JS = r"""
<script>
(() => {
  const algoDocModal = document.getElementById("algoDocModal");
  const openHeaderBtn = document.getElementById("openAlgoDocHeaderBtn");
  const openEmptyBtn = document.getElementById("openAlgoDocEmptyBtn");
  const closeTopBtn = document.getElementById("closeAlgoDocModalBtn");
  const doneBottomBtn = document.getElementById("doneAlgoDocModalBtn");
  const scrollBody = document.getElementById("algoDocScrollBody");

  const noMultiModal = document.getElementById("noMultiBpmModal");
  const openNoMultiHeaderBtn = document.getElementById("openNoMultiBpmHeaderBtn");
  const openNoMultiEmptyBtn = document.getElementById("openNoMultiBpmEmptyBtn");
  const closeNoMultiTopBtn = document.getElementById("closeNoMultiBpmModalBtn");
  const doneNoMultiBottomBtn = document.getElementById("doneNoMultiBpmModalBtn");
  const noMultiScrollBody = document.getElementById("noMultiBpmScrollBody");

  if (!algoDocModal && !noMultiModal) return;

  // MathML 浏览器排版能力探测：老旧浏览器无缝激活 .no-mathml 优雅降级占位
  function checkMathMLSupport() {
    try {
      if (typeof window.MathMLElement === "undefined") {
        return false;
      }
      const probe = document.createElement("div");
      probe.style.cssText = "position:absolute;top:-9999px;left:-9999px;visibility:hidden;font-size:24px;line-height:1;";
      probe.innerHTML = '<math xmlns="http://www.w3.org/1998/Math/MathML"><mfrac><mn>1</mn><mn>2</mn></mfrac></math>';
      document.body.appendChild(probe);
      const frac = probe.firstElementChild;
      const isRendered = frac && frac.getBoundingClientRect().height > 20;
      document.body.removeChild(probe);
      return Boolean(isRendered);
    } catch (_err) {
      return Boolean(typeof window.MathMLElement !== "undefined");
    }
  }

  if (!checkMathMLSupport()) {
    if (algoDocModal) algoDocModal.classList.add("no-mathml");
    if (noMultiModal) noMultiModal.classList.add("no-mathml");
    document.documentElement.classList.add("no-mathml");
  }

  function hideFloatingPopover() {
    const popover = document.getElementById("metricNotePopover");
    if (popover) {
      popover.classList.remove("visible");
      popover.setAttribute("aria-hidden", "true");
    }
  }

  function isOffsetSecondaryActive() {
    const offsetView = document.getElementById("offsetSecondaryView");
    return Boolean(offsetView && offsetView.classList.contains("active"));
  }

  const tabBtns = algoDocModal ? Array.from(algoDocModal.querySelectorAll(".algo-tab-btn[data-algo-tab]")) : [];
  const tabPanels = algoDocModal ? Array.from(algoDocModal.querySelectorAll("[data-algo-panel]")) : [];
  const jumpBtns = algoDocModal ? Array.from(algoDocModal.querySelectorAll("[data-algo-jump-tab]")) : [];

  function switchAlgoTab(tabKey) {
    tabBtns.forEach((btn) => {
      const active = btn.getAttribute("data-algo-tab") === tabKey;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    tabPanels.forEach((panel) => {
      const active = panel.getAttribute("data-algo-panel") === tabKey;
      panel.classList.toggle("active", active);
    });
    if (scrollBody) {
      scrollBody.scrollTop = 0;
    }
  }

  function closeAlgoDocModal() {
    if (algoDocModal) algoDocModal.classList.remove("open");
  }

  function closeNoMultiBpmModal() {
    if (noMultiModal) noMultiModal.classList.remove("open");
  }

  function openAlgoDocModal() {
    if (isOffsetSecondaryActive() || !algoDocModal) return;
    hideFloatingPopover();
    closeNoMultiBpmModal();
    algoDocModal.classList.add("open");
    if (scrollBody) {
      scrollBody.scrollTop = 0;
    }
  }

  function openNoMultiBpmModal() {
    if (isOffsetSecondaryActive() || !noMultiModal) return;
    hideFloatingPopover();
    closeAlgoDocModal();
    noMultiModal.classList.add("open");
    if (noMultiScrollBody) {
      noMultiScrollBody.scrollTop = 0;
    }
  }

  if (openHeaderBtn) openHeaderBtn.addEventListener("click", openAlgoDocModal);
  if (openEmptyBtn) openEmptyBtn.addEventListener("click", openAlgoDocModal);
  if (closeTopBtn) closeTopBtn.addEventListener("click", closeAlgoDocModal);
  if (doneBottomBtn) doneBottomBtn.addEventListener("click", closeAlgoDocModal);

  if (openNoMultiHeaderBtn) openNoMultiHeaderBtn.addEventListener("click", openNoMultiBpmModal);
  if (openNoMultiEmptyBtn) openNoMultiEmptyBtn.addEventListener("click", openNoMultiBpmModal);
  if (closeNoMultiTopBtn) closeNoMultiTopBtn.addEventListener("click", closeNoMultiBpmModal);
  if (doneNoMultiBottomBtn) doneNoMultiBottomBtn.addEventListener("click", closeNoMultiBpmModal);

  tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetTab = btn.getAttribute("data-algo-tab");
      if (targetTab) switchAlgoTab(targetTab);
    });
  });

  jumpBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetTab = btn.getAttribute("data-algo-jump-tab");
      if (targetTab) switchAlgoTab(targetTab);
    });
  });

  if (algoDocModal) {
    algoDocModal.addEventListener("click", (e) => {
      if (e.target === algoDocModal) closeAlgoDocModal();
    });
  }

  if (noMultiModal) {
    noMultiModal.addEventListener("click", (e) => {
      if (e.target === noMultiModal) closeNoMultiBpmModal();
    });
  }

  // 首页不提供多BPM说明弹窗：顶部目录锚点平滑快速直达（优先在弹窗内滚动，避免窗口抖动）
  const noMultiTocBtns = noMultiModal ? Array.from(noMultiModal.querySelectorAll("[data-no-multi-jump]")) : [];
  noMultiTocBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const jumpKey = btn.getAttribute("data-no-multi-jump");
      const targetMap = {
        sec1: "noMultiSec1",
        sec2: "noMultiSec2",
        sec3: "noMultiSec3",
        sec4: "noMultiSec4",
        sec5: "noMultiSec5",
        secFold: "noMultiSecFold",
      };
      const targetId = targetMap[jumpKey];
      if (!targetId) return;
      const targetEl = document.getElementById(targetId);
      if (!targetEl) return;
      if (targetEl.tagName && targetEl.tagName.toLowerCase() === "details" && !targetEl.hasAttribute("open")) {
        targetEl.setAttribute("open", "");
      }
      if (noMultiScrollBody) {
        const targetTop = targetEl.offsetTop - noMultiScrollBody.offsetTop;
        noMultiScrollBody.scrollTo({ top: Math.max(0, targetTop - 8), behavior: "smooth" });
      } else {
        targetEl.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });

  // 温馨提示第4项：联系作者号码点按复制（2026-11-11 新增）
  // 采用 document 级事件委托，即使空白页 DOM 被重绘也无需重新绑定；
  // 复用主页面全局 copyText(txt, el, ev)：写剪贴板后标签文字短暂变为“已复制 ✓”再还原。
  document.addEventListener("click", (e) => {
    const chip = e.target.closest ? e.target.closest(".contact-chip") : null;
    if (!chip) return;
    const txt = chip.getAttribute("data-copy") || "";
    if (!txt) return;
    if (typeof copyText === "function") {
      copyText(txt, chip, e);
    }
  });

  // 若进入 Offset 二级页面则自动收起两个说明弹窗，确保二级页面完全不出现该模块
  const offsetSecondaryEl = document.getElementById("offsetSecondaryView");
  if (offsetSecondaryEl && typeof MutationObserver !== "undefined") {
    const obs = new MutationObserver(() => {
      if (offsetSecondaryEl.classList.contains("active")) {
        if (algoDocModal && algoDocModal.classList.contains("open")) closeAlgoDocModal();
        if (noMultiModal && noMultiModal.classList.contains("open")) closeNoMultiBpmModal();
      }
    });
    obs.observe(offsetSecondaryEl, { attributes: true, attributeFilter: ["class"] });
  }

  // 捕获阶段键盘保护：Esc 仅关闭当前打开的说明弹窗，上下键/空格保留弹窗内原生滚动并阻断向底层切歌冒泡
  window.addEventListener(
    "keydown",
    (e) => {
      const algoOpen = Boolean(algoDocModal && algoDocModal.classList.contains("open"));
      const noMultiOpen = Boolean(noMultiModal && noMultiModal.classList.contains("open"));
      if (!algoOpen && !noMultiOpen) return;

      const exitModal = document.getElementById("confirmExitModal");
      if (exitModal && exitModal.classList.contains("open")) return;

      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (noMultiOpen) closeNoMultiBpmModal();
        if (algoOpen) closeAlgoDocModal();
        return;
      }
      if (e.key === "ArrowUp" || e.key === "ArrowDown" || e.code === "Space" || e.key === "c" || e.key === "C") {
        e.stopImmediatePropagation();
      }
    },
    true
  );
})();
</script>
"""


def inject_algo_doc_ui(index_html: str) -> str:
    """将顶栏与初始空白页并列双说明按钮、两个独立说明模态弹窗及控制器无侵入注入 INDEX_HTML。"""
    html = index_html

    if "</style>" not in html:
        raise RuntimeError("inject_algo_doc_ui 未找到 </style> 锚点，拒绝静默注入失败")
    html = html.replace("</style>", _ALGO_DOC_CSS + "\n</style>", 1)

    if _HEADER_CONTROLS_ANCHOR not in html:
        raise RuntimeError("inject_algo_doc_ui 未找到顶栏 .controls / #openSettingsBtn 锚点，拒绝静默注入失败")
    html = html.replace(
        _HEADER_CONTROLS_ANCHOR,
        '  <div class="controls">\n'
        + _HEADER_ALGO_DOC_BTN_HTML
        + '\n    <button type="button" class="header-combo-btn" id="openSettingsBtn"',
        1,
    )

    if _EMPTY_STATE_ANCHOR not in html:
        raise RuntimeError("inject_algo_doc_ui 未找到 #emptyState 锚点，拒绝静默注入失败")
    html = html.replace(_EMPTY_STATE_ANCHOR, _EMPTY_STATE_REPLACED_HTML, 1)

    if "</body>" not in html:
        raise RuntimeError("inject_algo_doc_ui 未找到 </body> 锚点，拒绝静默注入失败")
    html = html.replace(
        "</body>",
        _ALGO_DOC_MODAL_HTML + "\n" + _NO_MULTIBPM_MODAL_HTML + "\n" + _ALGO_DOC_JS + "\n</body>",
        1,
    )

    for bad_token in ("{{", "}}", "{%", "%}", "{#", "#}"):
        if (
            bad_token in _ALGO_DOC_CSS
            or bad_token in _ALGO_DOC_MODAL_HTML
            or bad_token in _NO_MULTIBPM_MODAL_HTML
            or bad_token in _ALGO_DOC_JS
        ):
            raise RuntimeError(f"inject_algo_doc_ui 检测到潜在 Jinja2 冲突定界符: {bad_token}")

    return html
