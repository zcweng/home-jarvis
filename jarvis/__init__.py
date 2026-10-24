"""home-jarvis：家庭版「理想同学」。

模块划分（一个进程 + 四个可替换适配器）：
    config.py       配置（.env 加载、设备名匹配参数）
    audio.py        录音 / 可打断播放
    vad.py          语音活动检测（断句）
    asr.py          语音识别（sherpa-onnx SenseVoice）
    kws.py          专用唤醒词（sherpa-onnx KeywordSpotter + 拼音 token）
    wake.py         唤醒门：续聊窗口 / 收线词
    tts.py          语音合成：macOS say（带音色体检）
    tts_neural.py   语音合成：本地 VITS（默认）
    textnorm.py     数字/单位 → 中文读法（神经 TTS 前置）
    output.py       输出路由：电脑喇叭 / 小爱音箱 / 两边
    llm.py          DeepSeek function calling
    intent.py       命令式意图判定（防「编造已执行」）
    tools.py        家电控制工具契约（HA REST）
    session.py      上下文 / 指代消解
    app.py          状态机
"""

__version__ = "0.1.0"
