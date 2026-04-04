"""
HITL(Human-in-the-Loop) 체크포인팅 테스트

SqliteSaver를 사용하여 interrupt 시점의 상태를 디스크에 저장하고,
서버(에이전트 프로세스)를 내렸다 올린 후에도 resume이 정상 동작하는지 검증합니다.

테스트 시나리오:
  1단계: 에이전트 실행 → execute 도구에서 interrupt 발생 → 상태 저장 확인 → 프로세스 종료
  2단계: 새 프로세스에서 에이전트 재생성 → 동일 thread_id로 resume → 실행 완료
  3단계: 다중 대화(multi-turn) - 완료 후 추가 대화 요청 → 다시 interrupt → resume
"""

import json
import os
import sys
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from deepagents import create_deep_agent
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from docker_sandbox import DockerSandboxBackend

# ─── 설정 ───
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "deepagents-sandbox")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "openai:gpt-5")
DB_PATH = "checkpoints.db"
THREAD_ID = "hitl-test-thread-001"

CONFIG = {"configurable": {"thread_id": THREAD_ID}}


def create_agent_with_checkpoint(db_path: str = DB_PATH):
    """SqliteSaver 체크포인터와 HITL interrupt가 설정된 에이전트 생성"""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    backend = DockerSandboxBackend(
        container_name=CONTAINER_NAME,
        workdir="/workspace",
    )
    agent = create_deep_agent(
        model=OPENAI_MODEL,
        backend=backend,
        checkpointer=checkpointer,
        interrupt_on={
            "execute": True,     # execute 도구 호출 시 interrupt
            "write_file": True,  # write_file 도구 호출 시 interrupt
        },
        system_prompt=(
            "당신은 엑셀 파일 전문가입니다. "
            "사용자의 요청에 따라 execute 도구로 Python 코드를 직접 실행하여 엑셀 파일을 만드세요. "
            "반드시 Excel 수식을 사용하세요. "
            "openpyxl 라이브러리가 이미 설치되어 있습니다. "
            "파일은 /workspace/output/ 디렉토리에 저장하세요. "
            "한국어로 응답하세요."
        ),
    )
    return agent, checkpointer


def inspect_checkpoint(db_path: str = DB_PATH):
    """SQLite DB에 저장된 체크포인트 상태를 조회"""
    if not Path(db_path).exists():
        print("  [DB] 체크포인트 DB 없음")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 테이블 목록 확인
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"  [DB] 테이블: {tables}")

    # checkpoints 테이블 행 수
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM [{table}]")
        count = cursor.fetchone()[0]
        print(f"  [DB] {table}: {count} rows")

    conn.close()
    return True


def print_interrupt_info(result):
    """interrupt 정보를 파싱하여 출력"""
    interrupts = result.tasks if hasattr(result, "tasks") else []
    for task in interrupts:
        if hasattr(task, "interrupts") and task.interrupts:
            for intr in task.interrupts:
                value = intr.value if hasattr(intr, "value") else intr
                print(f"\n  [INTERRUPT] 승인 요청:")
                if isinstance(value, dict):
                    for action in value.get("action_requests", []):
                        print(f"    도구: {action['name']}")
                        args_str = json.dumps(action.get("args", {}), ensure_ascii=False)
                        if len(args_str) > 200:
                            args_str = args_str[:200] + "..."
                        print(f"    인자: {args_str}")
                        if "description" in action:
                            desc = action["description"]
                            if len(desc) > 300:
                                desc = desc[:300] + "..."
                            print(f"    설명: {desc}")
                else:
                    print(f"    값: {value}")


# ═══════════════════════════════════════════════════════════════
# Phase 1: 첫 번째 실행 → interrupt 발생 → "서버 종료" 시뮬레이션
# ═══════════════════════════════════════════════════════════════

def phase1_invoke_and_interrupt():
    """에이전트 실행 → interrupt 발생 → 체크포인트 저장 확인"""
    print("=" * 70)
    print("PHASE 1: 에이전트 실행 → interrupt 발생 → 서버 종료 시뮬레이션")
    print("=" * 70)

    # 이전 체크포인트 DB 정리
    if Path(DB_PATH).exists():
        Path(DB_PATH).unlink()
        print("\n  [정리] 이전 체크포인트 DB 삭제")

    agent, checkpointer = create_agent_with_checkpoint()
    print("\n  [생성] 에이전트 + SqliteSaver 체크포인터 생성 완료")

    print("\n  [실행] 에이전트에게 엑셀 생성 요청 중...")
    print("         (execute 도구 호출 시 interrupt 예상)")

    # invoke 대신 get_state를 사용하기 위해 stream 사용
    result = agent.invoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "간단한 2024년 분기별 매출 엑셀을 만들어주세요.\n"
                        "- 제품: 스마트폰(150,200,180,220), 노트북(300,280,320,350)\n"
                        "- 헤더: 제품명, Q1, Q2, Q3, Q4, 합계\n"
                        "- 합계는 =SUM() 수식\n"
                        "- 파일: /workspace/output/test_sales.xlsx"
                    )
                )
            ]
        },
        config=CONFIG,
    )

    # 상태 확인
    state = agent.get_state(CONFIG)

    if state.tasks and any(
        hasattr(t, "interrupts") and t.interrupts for t in state.tasks
    ):
        print("\n  ✓ interrupt 발생 확인!")
        print_interrupt_info(state)

        # 체크포인트 DB 확인
        print("\n  [체크포인트 상태]")
        inspect_checkpoint()

        # 메시지 히스토리 확인
        msgs = state.values.get("messages", [])
        print(f"\n  [메시지] 저장된 메시지 수: {len(msgs)}")
        for i, msg in enumerate(msgs):
            role = type(msg).__name__
            content = msg.content if hasattr(msg, "content") else str(msg)
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            preview = content[:80] + "..." if len(str(content)) > 80 else content
            print(f"    [{i}] {role}: {preview}")

        print("\n  ──────────────────────────────────────")
        print("  서버 종료 시뮬레이션: 에이전트/체크포인터 객체 삭제")
        del agent
        del checkpointer
        print("  ✓ 에이전트 프로세스 메모리 해제 완료")
        print("  (실제 운영에서는 여기서 Pod/서버가 종료됨)")
        return True
    else:
        print("\n  ✗ interrupt가 발생하지 않았습니다.")
        print("    에이전트가 도구를 호출하지 않았거나 interrupt_on 설정이 매치되지 않았습니다.")

        # 결과 메시지 확인
        msgs = result.get("messages", []) if isinstance(result, dict) else []
        if msgs:
            last = msgs[-1]
            content = last.content if hasattr(last, "content") else str(last)
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            print(f"    마지막 메시지: {str(content)[:200]}")
        return False


# ═══════════════════════════════════════════════════════════════
# Phase 2: "서버 재시작" → 체크포인트에서 resume
# ═══════════════════════════════════════════════════════════════

def phase2_restart_and_resume():
    """새 에이전트 인스턴스로 체크포인트에서 resume"""
    print("\n" + "=" * 70)
    print("PHASE 2: 서버 재시작 시뮬레이션 → 체크포인트에서 resume")
    print("=" * 70)

    print("\n  [확인] 체크포인트 DB 존재 확인...")
    if not Path(DB_PATH).exists():
        print("  ✗ 체크포인트 DB가 없습니다!")
        return False

    inspect_checkpoint()

    # 완전히 새로운 에이전트 생성 (서버 재시작 시뮬레이션)
    print("\n  [재시작] 새 에이전트 + 체크포인터 생성 (= 새 서버/Pod)")
    agent, checkpointer = create_agent_with_checkpoint()

    # 저장된 상태 확인
    state = agent.get_state(CONFIG)
    msgs = state.values.get("messages", [])
    print(f"\n  [복원] 체크포인트에서 복원된 메시지 수: {len(msgs)}")

    if state.tasks and any(
        hasattr(t, "interrupts") and t.interrupts for t in state.tasks
    ):
        print("  ✓ 대기 중인 interrupt 확인!")
        print_interrupt_info(state)
    else:
        print("  ✗ 대기 중인 interrupt가 없습니다.")
        return False

    # resume - 승인(approve)
    print("\n  [RESUME] 사용자가 'approve' 결정을 내림...")
    print("  (실제에서는 웹 UI에서 승인 버튼 클릭)")

    # 승인 결과 전송
    resume_value = {"decisions": [{"type": "approve"}]}

    result = agent.invoke(
        Command(resume=resume_value),
        config=CONFIG,
    )

    # 결과 확인
    state = agent.get_state(CONFIG)

    if state.tasks and any(
        hasattr(t, "interrupts") and t.interrupts for t in state.tasks
    ):
        # 또 다른 interrupt 발생 (예: write_file)
        print("\n  ✓ 추가 interrupt 발생! (다음 도구 호출)")
        print_interrupt_info(state)

        # 연속 approve
        print("\n  [RESUME] 추가 승인 처리 중...")
        while True:
            state = agent.get_state(CONFIG)
            if not (
                state.tasks
                and any(hasattr(t, "interrupts") and t.interrupts for t in state.tasks)
            ):
                break

            print_interrupt_info(state)
            print("  → approve")
            result = agent.invoke(
                Command(resume={"decisions": [{"type": "approve"}]}),
                config=CONFIG,
            )

    # 최종 결과
    msgs = state.values.get("messages", [])
    print(f"\n  [완료] 최종 메시지 수: {len(msgs)}")
    last = msgs[-1] if msgs else None
    if last:
        content = last.content if hasattr(last, "content") else str(last)
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        print(f"  [최종 응답] {str(content)[:300]}")

    print("\n  ──────────────────────────────────────")
    print("  서버 종료 시뮬레이션 (2차)")
    del agent
    del checkpointer
    print("  ✓ 에이전트 프로세스 메모리 해제 완료")
    return True


# ═══════════════════════════════════════════════════════════════
# Phase 3: 다중 대화(multi-turn) - 재시작 후 이어서 대화
# ═══════════════════════════════════════════════════════════════

def phase3_multiturn_after_restart():
    """완료된 대화에 이어서 추가 요청 → interrupt → 재시작 → resume"""
    print("\n" + "=" * 70)
    print("PHASE 3: 다중 대화 - 서버 재시작 후 추가 대화 요청")
    print("=" * 70)

    print("\n  [재시작] 3번째 에이전트 인스턴스 생성")
    agent, checkpointer = create_agent_with_checkpoint()

    # 이전 대화 상태 확인
    state = agent.get_state(CONFIG)
    msgs = state.values.get("messages", [])
    print(f"\n  [복원] 이전 대화에서 복원된 메시지 수: {len(msgs)}")

    # 추가 대화 요청
    print("\n  [추가 요청] '시트 하나 더 추가해주세요' 대화 전송")
    result = agent.invoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "방금 만든 엑셀 파일에 '요약' 시트를 추가해주세요.\n"
                        "- 각 제품의 연간 합계만 표시\n"
                        "- 전체 매출 합계도 포함"
                    )
                )
            ]
        },
        config=CONFIG,
    )

    state = agent.get_state(CONFIG)

    if state.tasks and any(
        hasattr(t, "interrupts") and t.interrupts for t in state.tasks
    ):
        print("\n  ✓ 추가 대화에서도 interrupt 발생!")
        print_interrupt_info(state)

        # 서버 종료 시뮬레이션
        print("\n  ──────────────────────────────────────")
        print("  서버 종료 시뮬레이션 (3차)")
        del agent
        del checkpointer
        print("  ✓ 에이전트 프로세스 메모리 해제 완료")

        # 서버 재시작 → resume
        print("\n  [재시작] 4번째 에이전트 인스턴스 생성 → resume")
        agent, checkpointer = create_agent_with_checkpoint()

        state = agent.get_state(CONFIG)
        if state.tasks and any(
            hasattr(t, "interrupts") and t.interrupts for t in state.tasks
        ):
            print("  ✓ 체크포인트에서 interrupt 상태 복원 확인!")

            # 모든 pending interrupt 승인
            while True:
                state = agent.get_state(CONFIG)
                if not (
                    state.tasks
                    and any(
                        hasattr(t, "interrupts") and t.interrupts for t in state.tasks
                    )
                ):
                    break

                print_interrupt_info(state)
                print("  → approve")
                result = agent.invoke(
                    Command(resume={"decisions": [{"type": "approve"}]}),
                    config=CONFIG,
                )

        state = agent.get_state(CONFIG)
        msgs = state.values.get("messages", [])
        print(f"\n  [완료] 전체 대화 메시지 수: {len(msgs)}")
        last = msgs[-1] if msgs else None
        if last:
            content = last.content if hasattr(last, "content") else str(last)
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            print(f"  [최종 응답] {str(content)[:300]}")

        del agent
        del checkpointer
        return True
    else:
        print("\n  (interrupt 없이 완료됨 - 도구를 호출하지 않은 경우)")
        msgs = state.values.get("messages", [])
        print(f"  [완료] 전체 대화 메시지 수: {len(msgs)}")
        del agent
        del checkpointer
        return True


# ═══════════════════════════════════════════════════════════════
# 메인 실행
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n🔬 HITL 체크포인팅 테스트 시작")
    print("   - Checkpointer: SqliteSaver (디스크 기반)")
    print(f"   - DB 파일: {DB_PATH}")
    print(f"   - Thread ID: {THREAD_ID}")
    print(f"   - Model: {OPENAI_MODEL}")
    print(f"   - interrupt_on: execute, write_file")
    print()

    # Phase 1
    phase1_ok = phase1_invoke_and_interrupt()
    if not phase1_ok:
        print("\n❌ Phase 1 실패 - interrupt가 발생하지 않아 테스트 중단")
        sys.exit(1)

    # Phase 2
    phase2_ok = phase2_restart_and_resume()
    if not phase2_ok:
        print("\n❌ Phase 2 실패 - resume이 동작하지 않음")
        sys.exit(1)

    # Phase 3
    phase3_ok = phase3_multiturn_after_restart()

    # 결과 요약
    print("\n" + "=" * 70)
    print("테스트 결과 요약")
    print("=" * 70)
    print(f"  Phase 1 (invoke → interrupt → 서버 종료):     {'✓ PASS' if phase1_ok else '✗ FAIL'}")
    print(f"  Phase 2 (서버 재시작 → resume):               {'✓ PASS' if phase2_ok else '✗ FAIL'}")
    print(f"  Phase 3 (다중 대화 → interrupt → 재시작):     {'✓ PASS' if phase3_ok else '✗ FAIL'}")

    # 최종 체크포인트 상태
    print("\n  [최종 DB 상태]")
    inspect_checkpoint()

    # 정리
    if Path(DB_PATH).exists():
        size = Path(DB_PATH).stat().st_size
        print(f"\n  체크포인트 DB 크기: {size / 1024:.1f} KB")

    all_pass = phase1_ok and phase2_ok and phase3_ok
    print(f"\n{'✅ 모든 테스트 통과!' if all_pass else '❌ 일부 테스트 실패'}")
    sys.exit(0 if all_pass else 1)
