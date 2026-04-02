"""
FastAPI 백엔드 - Deep Agent 대화형 SSE 스트리밍 + 파일 업로드
StreamingResponse로 직접 SSE를 flush하여 실시간 전달
"""

import asyncio
import json
import os
import re
import subprocess
import threading
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from dotenv import load_dotenv

from docker_sandbox import DockerSandboxBackend

load_dotenv()

CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "deepagents-sandbox")
SANDBOX_WORKDIR = os.environ.get("SANDBOX_WORKDIR", "/workspace")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "openai:gpt-5")

_agent = None
_sessions: dict[str, list] = {}


def get_agent():
    global _agent
    if _agent is None:
        from deepagents import create_deep_agent

        backend = DockerSandboxBackend(
            container_name=CONTAINER_NAME,
            workdir=SANDBOX_WORKDIR,
        )
        _agent = create_deep_agent(
            model=OPENAI_MODEL,
            skills=["skills/"],
            backend=backend,
            system_prompt=(
                "당신은 엑셀 파일 전문가입니다. "
                "사용자의 요청에 따라 execute 도구로 Python 코드를 직접 실행하여 엑셀 파일을 만들거나, "
                "사용자가 업로드한 /workspace/uploads/ 의 파일을 분석/편집할 수 있습니다.\n\n"
                "## 응답 스타일 (매우 중요)\n"
                "도구를 호출하기 전에, 반드시 먼저 한국어로 지금 무엇을 할 것인지 "
                "1~2문장으로 설명한 후 도구를 호출하세요. 예시:\n"
                '- "먼저 업로드된 파일 목록을 확인하겠습니다."\n'
                '- "PDF에서 테이블 구조를 추출하겠습니다."\n'
                '- "추출된 데이터를 기반으로 엑셀 파일을 생성하겠습니다."\n'
                '- "수식을 검증하기 위해 recalc.py를 실행하겠습니다."\n'
                "도구 결과를 받은 후에도 결과를 간단히 요약해주세요. "
                "절대로 설명 없이 도구만 연속 호출하지 마세요.\n\n"
                "## 중요 규칙\n"
                "- 반드시 Excel 수식을 사용하고, Python에서 값을 계산하여 하드코딩하지 마세요.\n"
                "- openpyxl, pandas, pdfplumber 라이브러리가 설치되어 있습니다.\n"
                "- PDF 파일은 바이너리이므로 read_file 도구로 읽지 마세요. "
                "반드시 execute 도구로 pdfplumber를 사용하여 읽으세요:\n"
                "  ```python\nimport pdfplumber\nwith pdfplumber.open('/workspace/uploads/파일.pdf') as pdf:\n"
                "    for page in pdf.pages:\n        print(page.extract_text())\n```\n"
                "- 수식이 포함된 엑셀 파일을 만든 후에는 반드시 "
                "python /workspace/skills/xlsx/scripts/recalc.py <파일경로> 를 실행하여 "
                "수식 값을 재계산하고 오류를 검증하세요.\n"
                "- 파일은 /workspace/output/ 디렉토리에 저장하세요.\n"
                "- 한국어로 응답하세요."
            ),
        )
    return _agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_agent()
    subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "mkdir", "-p",
         "/workspace/uploads", "/workspace/output"],
        capture_output=True,
    )
    yield


app = FastAPI(title="Deep Agent Excel Generator", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── SSE 헬퍼 ───

def _format_sse(event: str, data: dict) -> str:
    """SSE 프로토콜 문자열 생성 (즉시 flush용)"""
    encoded = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {encoded}\n\n"


# ─── 파일 업로드 ───


@app.post("/api/upload")
async def upload_files(
    files: list[UploadFile] = File(...),
    session_id: str = Form(""),
):
    uploaded = []
    for f in files:
        content = await f.read()
        filename = unicodedata.normalize("NFC", f.filename or "unknown")
        container_path = f"/workspace/uploads/{filename}"
        tmp_name = f"/tmp/_upload_{uuid.uuid4().hex}"
        Path(tmp_name).write_bytes(content)
        tmp_container = f"/workspace/uploads/_tmp_{uuid.uuid4().hex}"
        result = subprocess.run(
            ["docker", "cp", tmp_name, f"{CONTAINER_NAME}:{tmp_container}"],
            capture_output=True,
        )
        Path(tmp_name).unlink(missing_ok=True)
        if result.returncode == 0:
            subprocess.run(
                ["docker", "exec", CONTAINER_NAME, "mv", tmp_container, container_path],
                capture_output=True,
            )
            uploaded.append({"name": filename, "path": container_path, "size": len(content)})
        else:
            uploaded.append({"name": filename, "error": result.stderr.decode()})
    return {"uploaded": uploaded}


# ─── 파일 목록 ───


@app.get("/api/files")
async def list_files():
    result = subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "bash", "-c",
         "echo '=== uploads ===' && ls -lh /workspace/uploads/ 2>/dev/null && "
         "echo '=== output ===' && ls -lh /workspace/output/ 2>/dev/null"],
        capture_output=True, text=True,
    )
    files = {"uploads": [], "output": []}
    current = None
    for line in result.stdout.strip().split("\n"):
        if "=== uploads ===" in line:
            current = "uploads"
            continue
        elif "=== output ===" in line:
            current = "output"
            continue
        if current and not line.startswith("total"):
            parts = line.split()
            if len(parts) >= 9:
                files[current].append({
                    "name": " ".join(parts[8:]),
                    "size": parts[4],
                })
    return files


# ─── 파일 다운로드 ───


@app.get("/api/download/{filename:path}")
async def download_file(filename: str):
    local_path = f"/tmp/_dl_{uuid.uuid4().hex}_{Path(filename).name}"
    container_path = f"/workspace/output/{filename}"
    result = subprocess.run(
        ["docker", "cp", f"{CONTAINER_NAME}:{container_path}", local_path],
        capture_output=True,
    )
    if result.returncode == 0:
        return FileResponse(
            local_path,
            media_type="application/octet-stream",
            filename=Path(filename).name,
        )
    return {"error": f"파일을 찾을 수 없습니다: {filename}"}


# ─── SSE 스트리밍 (StreamingResponse 직접 사용) ───

_SENTINEL = object()


def _run_agent_in_thread(prompt: str, session_id: str, aq: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """별도 스레드에서 동기 agent.stream() → asyncio.Queue 로 전달"""
    try:
        ag = get_agent()
        if session_id not in _sessions:
            _sessions[session_id] = []
        history = _sessions[session_id]
        history.append(HumanMessage(content=prompt))

        for mode, payload in ag.stream(
            {"messages": list(history)},
            stream_mode=["messages", "updates"],
        ):
            loop.call_soon_threadsafe(aq.put_nowait, (mode, payload))
    except Exception as e:
        loop.call_soon_threadsafe(aq.put_nowait, ("__error__", str(e)))
    finally:
        loop.call_soon_threadsafe(aq.put_nowait, _SENTINEL)


async def _generate_sse(prompt: str, session_id: str):
    """asyncio.Queue 에서 이벤트를 꺼내 SSE 문자열로 즉시 yield"""
    aq: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    thread = threading.Thread(
        target=_run_agent_in_thread,
        args=(prompt, session_id, aq, loop),
        daemon=True,
    )
    thread.start()

    # 첫 이벤트 즉시 전송
    yield _format_sse("status", {"message": "에이전트 실행 시작..."})

    generated_files = []
    seen_skills = set()
    seen_refs = set()  # 참조된 파일
    pending_tool_names = {}  # tool_call_id -> name (todo 파싱용)

    while True:
        item = await aq.get()

        if item is _SENTINEL:
            break

        mode, payload = item

        if mode == "__error__":
            yield _format_sse("error", {"message": str(payload)})
            break

        if mode == "messages":
            msg_chunk, metadata = payload
            node = metadata.get("langgraph_node", "")

            if isinstance(msg_chunk, AIMessageChunk):
                # 텍스트 토큰
                if msg_chunk.content:
                    text = ""
                    if isinstance(msg_chunk.content, str):
                        text = msg_chunk.content
                    elif isinstance(msg_chunk.content, list):
                        for block in msg_chunk.content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text += block.get("text", "")
                            elif isinstance(block, str):
                                text += block
                    if text:
                        yield _format_sse("token", {"text": text, "node": node})

                # 도구 호출 시작
                if msg_chunk.tool_call_chunks:
                    for tc in msg_chunk.tool_call_chunks:
                        tc_id = tc.get("id") or tc.get("index", "")
                        if tc_id and tc.get("name"):
                            pending_tool_names[tc_id] = tc["name"]
                            yield _format_sse(
                                "tool_start",
                                {"tool_call_id": str(tc_id), "name": tc["name"], "node": node},
                            )

            # 도구 결과
            elif isinstance(msg_chunk, ToolMessage):
                content = msg_chunk.content
                if not isinstance(content, str):
                    content = str(content)
                tool_name = msg_chunk.name or pending_tool_names.get(msg_chunk.tool_call_id, "")

                # ── todo 이벤트 감지 (write_todos 도구) ──
                if tool_name == "write_todos":
                    todos = []
                    try:
                        # "Updated todo list to [{...}, ...]" 형태에서 리스트 부분 추출
                        import ast
                        bracket_start = content.find("[")
                        if bracket_start >= 0:
                            list_str = content[bracket_start:]
                            todos = ast.literal_eval(list_str)
                    except Exception:
                        pass
                    if todos:
                        yield _format_sse("todos", {"items": todos})

                # ── 참조 파일 감지 (ls, read_file 등) ──
                if tool_name in ("ls", "read_file", "glob"):
                    # 따옴표로 감싼 경로 추출 (공백 포함 파일명 대응)
                    file_matches = re.findall(r"/workspace/uploads/[^'\"\]\n]+", content)
                    for fm in file_matches:
                        fm = fm.rstrip(" ,")
                        fname = fm.split("/workspace/uploads/")[-1]
                        if fname and fname not in seen_refs:
                            seen_refs.add(fname)
                            yield _format_sse("ref_file", {"name": fname, "path": fm})

                # ── 생성 파일 감지 ──
                if "/workspace/output/" in content:
                    found = re.findall(r"/workspace/output/[\w\-\.]+", content)
                    for fp in found:
                        fname = Path(fp).name
                        if fname not in generated_files:
                            generated_files.append(fname)

                yield _format_sse(
                    "tool_result",
                    {
                        "tool_call_id": msg_chunk.tool_call_id or "",
                        "name": tool_name,
                        "content": content[:3000],
                        "node": node,
                    },
                )

        elif mode == "updates":
            for node_name in payload:
                if node_name.startswith("__"):
                    continue
                # 스킬 로드 감지
                if "SkillsMiddleware" in node_name and node_name not in seen_skills:
                    seen_skills.add(node_name)
                    yield _format_sse("skill_loaded", {"name": "xlsx"})
                yield _format_sse("node_update", {"node": node_name})

    # output 디렉토리 스캔
    scan = subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "ls", "/workspace/output/"],
        capture_output=True, text=True,
    )
    all_files = [f for f in scan.stdout.strip().split("\n") if f] if scan.returncode == 0 else generated_files

    yield _format_sse("done", {"message": "완료!", "files": all_files})


@app.get("/api/stream")
async def stream_endpoint(prompt: str = "", session_id: str = "default"):
    if not prompt:
        return {"error": "prompt is required"}
    return StreamingResponse(
        _generate_sse(prompt, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 프록시 버퍼링 방지
        },
    )


# ─── 세션 초기화 ───


@app.post("/api/session/reset")
async def reset_session(session_id: str = "default"):
    _sessions.pop(session_id, None)
    subprocess.run(
        ["docker", "exec", CONTAINER_NAME, "bash", "-c",
         "rm -rf /workspace/output/* /workspace/uploads/*"],
        capture_output=True,
    )
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("BACKEND_HOST", "0.0.0.0")
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
