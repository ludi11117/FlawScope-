from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional
import os


class Settings(BaseSettings):
    # API Keys
    SILICONFLOW_API_KEY: str = Field(..., description="SiliconFlow API Key")
    SILICONFLOW_BASE_URL: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="SiliconFlow API Base URL"
    )
    JUDGE_BASE_URL: Optional[str] = Field(
        default=None,
        description="判官模型专用 Base URL (可选，默认复用 SILICONFLOW_BASE_URL)"
    )

    # Models
    DIAGNOSIS_MODEL: str = Field(
        default="Qwen/Qwen2.5-14B-Instruct",
        description="主诊断模型"
    )
    JUDGE_MODEL: str = Field(
        default="Qwen/Qwen2.5-72B-Instruct",
        description="判官模型 (建议不同厂商/规模)"
    )
    VISION_MODEL: str = Field(
        default="Qwen/Qwen2.5-VL-7B-Instruct",
        description="视觉模型"
    )
    EMBEDDING_MODEL: str = Field(
        default="BAAI/bge-m3",
        description="嵌入模型"
    )

    # LLM Parameters
    LLM_TEMPERATURE: float = Field(default=0.1, ge=0.0, le=2.0)
    LLM_REQUEST_TIMEOUT: int = Field(default=60, ge=10, le=300)
    LLM_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    LLM_RETRY_BACKOFF: float = Field(default=1.5, description="指数退避基数")

    # Orchestration
    MAX_DEBATE_ROUNDS: int = Field(default=3, ge=1, le=10)
    RETRIEVAL_K: int = Field(default=3, ge=1, le=10)
    BM25_K: int = Field(default=3, ge=1, le=10)
    RRF_K: int = Field(default=60, ge=1, le=1000, description="RRF 融合平滑系数，越大越弱化高排名优势")
    # LangGraph 图执行步数上限。不显式设置时用的是框架默认值 10007，
    # 意味着最坏情况能执行上万次节点、成倍烧掉 LLM 配额才报错。
    # 正常链路约 9~15 步，辩论环最多 3 轮，100 已是宽裕上限。
    GRAPH_RECURSION_LIMIT: int = Field(default=100, ge=20, le=1000, description="图执行最大步数")

    # Cost Calculation
    LABOR_RATE_PER_HOUR: int = Field(default=150, ge=0)
    PARTS_PRICE: dict = Field(
        default={
            "主轴轴承": 850,
            "润滑脂": 80,
            "冷却风扇": 300,
            "液压泵": 2500,
            "溢流阀": 600,
            "液压油": 200,
            "编码器": 1200,
            "联轴器": 450,
            "密封件": 120,
            "空气过滤器滤芯": 260,
            "油分离器滤芯": 380,
            "温控阀": 520,
            "制冷剂": 160,
            "膨胀阀": 680,
            "冷却塔填料": 320,
            "母线滤波电容": 900,
            "主接触器": 750,
            "减速机润滑脂": 340,
            "骨架油封": 90,
            "齿轮油": 180,
            "气缸密封圈": 110,
            "电磁阀": 420,
            "减压阀": 460,
            "滤袋": 150,
            "脉冲阀膜片": 240,
            "托辊": 280,
            "滚筒包胶": 1400,
            "底阀": 360,
            "机械密封": 620,
            # 2026-09-20 扩库新增：配套「无报警代码」症状章节涉及的可更换件
            "主轴皮带": 180,
            "刀柄拉钉": 90,
            "进气阀片": 320,
            "减震垫": 60,
        },
        description="备件价格表 (可通过环境变量 JSON 覆盖)"
    )

    # Database
    DB_PATH: str = Field(default="diagnosis_history.db")
    CHROMA_PERSIST_DIR: str = Field(default="chroma_db")

    # Logging
    LOG_LEVEL: str = Field(default="INFO")
    LOG_JSON: bool = Field(default=False, description="是否输出 JSON 格式日志")

    # API
    API_HOST: str = Field(default="0.0.0.0")
    API_PORT: int = Field(default=8000)
    HEALTH_CACHE_TTL: int = Field(
        default=60,
        ge=0,
        description="健康检查结果缓存秒数；0 表示每次都真探。避免高频探活反复打 LLM"
    )
    MAX_CONCURRENT_DIAGNOSES: int = Field(
        default=4,
        ge=1,
        le=64,
        description=(
            "同时在跑的诊断上限。一次诊断要串行走 6~9 次 LLM 调用、耗时数十秒，"
            "不设上限时突发流量会把 FastAPI 线程池占满（连 /health 都会排队），"
            "并成倍放大模型配额消耗。超限的请求直接返回 503，而不是无声排队"
        )
    )

    # 安全
    API_KEY: Optional[str] = Field(
        default=None,
        description="可选 API Key。设置后业务接口需带 X-API-Key 请求头；留空则不做鉴权（本地开发默认）"
    )
    MAX_IMAGE_BASE64_CHARS: int = Field(
        default=8_000_000,
        ge=1000,
        description="单张图片 base64 字符串长度上限（约 6MB 原图）。此前该字段无上限，超大请求可打爆内存与配额"
    )
    RETRIEVAL_DEVICE_FILTER: bool = Field(
        default=False,
        description=(
            "检索时是否按设备 metadata 过滤。**默认关闭**，三个理由："
            "① 现有 chroma_db 是没写 metadata 的旧库，开启会直接查不到东西 → 召回为空 → 误降级；"
            "② 一刀切过滤有真实风险——用户写「空压机」而库里章节名是「空气压缩机」时，"
            "本来能召回的证据会被整段滤掉；"
            "③ 更合适的形态是「降级前的二次尝试」（先不带过滤检索，为空或不匹配时再带过滤重试），"
            "而那会牵动误降级指标，需要先做产品决策。"
            "开启前必须先用 build_knowledge_base.py 重建向量库以写入 metadata"
        )
    )
    CORS_ALLOW_ORIGINS: list = Field(
        default=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8501",
            "http://127.0.0.1:8501",
        ],
        description=(
            "允许跨域的前端来源。5173 是 Vite 开发服务器（当前唯一前端）；"
            "8501 仅为本地起 Streamlit 旧前端做对照时保留，容器不再暴露该端口。"
            "刻意不用 ['*']：带凭据的请求下通配符会被浏览器拒绝，"
            "生产环境放开任意源则等于把 API 暴露给任何网站。"
            "公网部署时把实际前端域名追加进来"
        )
    )

    # 用 SettingsConfigDict 而不是 `class Config`：后者在 Pydantic v2 里已废弃
    # （每次导入都会打一条 PydanticDeprecatedSince20），v3 会直接移除。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 允许通过环境变量 JSON 覆盖 PARTS_PRICE
        parts_price_env = os.getenv("PARTS_PRICE_JSON")
        if parts_price_env:
            import json
            try:
                self.PARTS_PRICE = json.loads(parts_price_env)
            except json.JSONDecodeError:
                pass


settings = Settings()


# 导出常用单例，保持向后兼容
SILICONFLOW_API_KEY = settings.SILICONFLOW_API_KEY
SILICONFLOW_BASE_URL = settings.SILICONFLOW_BASE_URL
DIAGNOSIS_MODEL = settings.DIAGNOSIS_MODEL
JUDGE_MODEL = settings.JUDGE_MODEL
VISION_MODEL = settings.VISION_MODEL
EMBEDDING_MODEL = settings.EMBEDDING_MODEL
MAX_DEBATE_ROUNDS = settings.MAX_DEBATE_ROUNDS
RETRIEVAL_K = settings.RETRIEVAL_K
GRAPH_RECURSION_LIMIT = settings.GRAPH_RECURSION_LIMIT
LABOR_RATE_PER_HOUR = settings.LABOR_RATE_PER_HOUR
PARTS_PRICE = settings.PARTS_PRICE
DB_PATH = settings.DB_PATH