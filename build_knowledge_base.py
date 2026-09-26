"""构建 / 重建向量知识库。

用法：
    venv\\Scripts\\python.exe build_knowledge_base.py              # 重建（默认：先清空再写入）
    venv\\Scripts\\python.exe build_knowledge_base.py --dry-run    # 只看切分结果，不写库、不调模型
    venv\\Scripts\\python.exe build_knowledge_base.py --append     # 追加写入，不清空

为什么默认是「清空重建」而不是「追加」：
    ``Chroma.from_texts`` 是**追加**语义——集合已存在时它把新文档 add 进去。
    这个脚本此前就是直接调 from_texts，于是每次重跑，库里的块数都翻一倍：
    改完 data/knowledge_base.txt 再跑一次，检索结果里全是近重复条目，
    既白烧 context，又让 RRF 融合被同源文档刷屏（同一份资料占掉多个名额）。
    重建是幂等的（跑 N 次结果一致），追加不是——所以默认选前者。

路径统一取 settings，不再写死：
    - 之前 persist_directory 硬编码成 "chroma_db"，绕过了 settings.CHROMA_PERSIST_DIR。
      容器里 compose 会把 /app/chroma_db 挂载出来，两边一旦不一致，
      就会写出一个"跑在容器里、宿主机看不到"的库。
    - 知识库原文也改成相对本文件定位，之前用相对 cwd 的路径，
      换个目录执行就 FileNotFoundError。
"""

import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from agents import knowledge_base_health
from config import settings
from logging_config import configure_logging, get_logger

load_dotenv()
configure_logging()

logger = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent
KB_PATH = BASE_DIR / "data" / "knowledge_base.txt"

CHUNK_SIZE = 200
CHUNK_OVERLAP = 30

# 只保留「【设备名常见故障与排查】」及其之后的内容。
# 文件头是**给人看的元信息**（标题、来源说明、免责声明），不该参与检索：
# 它会被切成 200 字的碎片，而检索名额只有 RETRIEVAL_K=3 个，
# 于是「# FlawScope 知识库 —— 维修经验整理」「---」「> 而这正是本项目…」这类
# 碎片会挤掉真正的故障条目。实测（2026-09-18）三个查询里各有 1–2 个名额被这样浪费。
_SECTION_START = re.compile(r"^【.+常见故障与排查】\s*$", re.MULTILINE)


def strip_preamble(text: str) -> str:
    """剥掉文件头，只留设备章节正文。找不到章节标题时原样返回（不静默丢内容）。"""
    m = _SECTION_START.search(text)
    if not m:
        return text
    return text[m.start():]


def _count(db: Chroma) -> int:
    """当前集合里的块数。取不到就返回 -1，只用于打印，不该让脚本失败。"""
    try:
        return len(db.get()["ids"])
    except Exception:
        return -1


# 设备章节标题形如「【数控机床主轴电机常见故障与排查】」，
# 去掉这个后缀就是设备名（与 `_SECTION_START` 的正则保持一致）。
_SECTION_SUFFIX = "常见故障与排查"
_SECTION_TITLE = re.compile(r"^【(.+?)】\s*$", re.MULTILINE)
_CHAPTER_TITLE = re.compile(r"^[一二三四五六七八九十]+、\s*(.+)$", re.MULTILINE)
_ALARM_CODE = re.compile(r"([A-Za-z]-\d{3})")


def _device_of(section_title: str) -> str:
    name = section_title
    if name.endswith(_SECTION_SUFFIX):
        name = name[: -len(_SECTION_SUFFIX)]
    return name.strip()


def parse_chunk_metadata(chunks: list, body: str) -> list:
    """给每个块补上来源 metadata：``{section, chapter, alarm, device}``。

    **不改动切分本身**。做法是按位置回溯：`RecursiveCharacterTextSplitter` 的输出
    保持原文顺序，所以从前往后找每个块在原文里的起点（游标只前进，重叠的块也能
    正确定位），再取它之前最近的一个设备章节标题与条目标题即可。块边界与以前
    逐字节相同，只是给已有的块补上"它来自哪"。

    为什么需要它：没有 metadata 时，`retrieve_evidence` 既无法按设备过滤，
    也无法把命中块溯源到"哪个设备 / 哪条原因"——README 把"条目级切分"列为待办，
    而 metadata 是它的前置条件。也给 C1 那条设备护栏留了条比"词面匹配"更硬的退路。

    字段说明：
      - `section`：设备章节标题（【…常见故障与排查】那一行）。**只有这里**才一定
        含设备名——数控机床章节的条目标题写的是"主轴电机"。
      - `chapter`：条目标题（「一、…」）。README 的"条目级切分"要用它。
        任务书只列了三个键，这是额外的一个；不加它就得在检索层重新解析文本。
      - `alarm`：条目标题里的报警代码，没有则为空串。
      - `device`：设备名（章节标题去掉后缀）。
    """
    metadatas = []
    cursor = 0
    for chunk in chunks:
        idx = body.find(chunk, cursor)
        if idx < 0:
            # 找不到就退回"从当前位置往前看"：宁可 metadata 粗一点，
            # 也不能让整个构建过程挂掉（构建失败比字段不准严重得多）。
            idx = cursor
        cursor = idx + 1

        # 先看块**自己开头**有没有标题。块边界常常正好切在标题之前，
        # 此时标题不在 `body[:idx]` 里，但它是这一块自己的来源。
        head_section = _SECTION_TITLE.match(chunk)
        head_chapter = _CHAPTER_TITLE.match(chunk)

        prefix = body[:idx]
        if head_section:
            section_title = head_section.group(1).strip()
            # 章节头那一块不属于任何条目
            chapter_title = ""
        else:
            last_section = None
            for last_section in _SECTION_TITLE.finditer(prefix):
                pass
            section_title = last_section.group(1).strip() if last_section else ""

            if head_chapter:
                chapter_title = head_chapter.group(1).strip()
            else:
                last_chapter = None
                for last_chapter in _CHAPTER_TITLE.finditer(prefix):
                    pass
                chapter_title = last_chapter.group(1).strip() if last_chapter else ""

        alarm_match = _ALARM_CODE.search(chapter_title)

        metadatas.append({
            "section": section_title,
            "chapter": chapter_title,
            "alarm": alarm_match.group(1) if alarm_match else "",
            "device": _device_of(section_title),
        })
    return metadatas


def build(dry_run: bool = False, append: bool = False, force: bool = False) -> int:
    if not KB_PATH.exists():
        print(f"找不到知识库原文：{KB_PATH}")
        return 1

    text = KB_PATH.read_text(encoding="utf-8")
    body = strip_preamble(text)
    if len(body) < len(text):
        print(f"已剥离文件头 {len(text) - len(body)} 字符（说明性内容不参与检索）")

    # ---- 格式守卫：必须在任何破坏性操作之前跑 ----
    # 为什么放在 reset_collection() 之前：本脚本是"先清后写"，格式不合格时若先清，
    # 就会留下一个空库（此坑已踩过一次）。格式不对应当在动库之前就拦下。
    health = knowledge_base_health(body)
    print(f"格式自检：解析出 {health['entries']} 条『可能原因』，覆盖 {health['section_count']} 个章节")
    if not health["ok"] and not force:
        print()
        print("=" * 68)
        print("✗ 知识库格式不合格：未能解析出任何『可能原因』条目。")
        print()
        print("  这不是小事——排除条件映射会**静默失效**：系统照样能诊断，")
        print("  但用户说的『已排除某某』不再被遵守，而且不会有任何报错。")
        print()
        print("  请按 docs/数据接入契约.md 调整格式，常见写法均可：")
        print("    · 章节行：  『一、设备名』  或  『## 设备名』")
        print("    · 原因条目：『1. 原因』『1、原因』『- 原因』『可能原因：原因』")
        print()
        print("  调整后先跑自查： python tools/validate_knowledge_base.py")
        print("  确认要带着不合格格式继续构建：加 --force")
        print("=" * 68)
        return 1

    chunks = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    ).split_text(body)
    print(f"原文 {len(text)} 字符 → 正文 {len(body)} 字符 → 切分为 {len(chunks)} 块")

    # 入库时写 metadata：给检索层留出"按设备/报警码过滤 + 命中块溯源"的能力。
    # 这一步**不改变切分结果**（见 parse_chunk_metadata），所以对现有检索行为
    # 是零影响；只是让库里的块多带几个字段。
    metadatas = parse_chunk_metadata(chunks, body)
    labeled = sum(1 for m in metadatas if m["section"])
    print(f"已为 {labeled}/{len(chunks)} 块解析出来源（设备章节 / 条目 / 报警代码）")

    if dry_run:
        for i, chunk in enumerate(chunks[:5], start=1):
            print(f"  [{i}] {chunk[:60]!r}  meta={metadatas[i - 1]}")
        if len(chunks) > 5:
            print(f"  ...（其余 {len(chunks) - 5} 块省略）")
        print("--dry-run：未写入向量库，也未调用嵌入模型")
        return 0

    if not settings.SILICONFLOW_API_KEY:
        print("缺少 SILICONFLOW_API_KEY，无法生成嵌入。请在 .env 中配置后重试。")
        return 1

    embeddings = OpenAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        openai_api_key=settings.SILICONFLOW_API_KEY,
        openai_api_base=settings.SILICONFLOW_BASE_URL,
    )

    db = Chroma(
        persist_directory=settings.CHROMA_PERSIST_DIR,
        embedding_function=embeddings,
    )
    before = _count(db)

    if append:
        db.add_texts(chunks, metadatas=metadatas)
        action = "追加"
    else:
        # 先删集合并重建空集合，再写入 —— 这样重复执行结果稳定
        db.reset_collection()
        db.add_texts(chunks, metadatas=metadatas)
        action = "重建"

    after = _count(db)
    print(f"{action}完成：{before} 块 → {after} 块")
    print(f"已保存到 {Path(settings.CHROMA_PERSIST_DIR).resolve()}")
    print("提示：正在运行的服务进程仍持有旧索引，需重启后端才会加载新的知识库。")
    logger.info("knowledge_base_built", action=action, before=before, after=after, chunks=len(chunks))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 / 重建向量知识库")
    parser.add_argument("--dry-run", action="store_true", help="只做切分与预览，不写库、不调模型")
    parser.add_argument("--append", action="store_true", help="追加写入而不清空（默认清空重建）")
    parser.add_argument("--force", action="store_true",
                        help="格式自检不合格时仍然继续（不推荐：排除映射会静默失效）")
    args = parser.parse_args()
    return build(dry_run=args.dry_run, append=args.append, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
