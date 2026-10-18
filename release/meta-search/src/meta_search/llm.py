import json
import re
import subprocess
import time
from typing import Callable

import jsonschema
from pydantic import ValidationError as PydanticValidationError

from .outputs import OUTPUTS
from .validation import PipelineError, ValidationError


class LLMError(RuntimeError):
    pass


def render(template, **values):
    def replace(match):
        key = match.group(1)
        if key not in values:
            raise ValidationError(f"프롬프트 자리표시자 누락: {key}")
        value = values[key]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)

    return re.sub(r"\{\{(\w+)\}\}", replace, template)


def _save_output(ctx, step, stream, value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    (ctx.job_dir / f"{step}.{stream}.txt").write_text(value or "", encoding="utf-8")


def _execute(cmd, ctx, step, **kwargs):
    start = time.perf_counter()
    ctx.emit("process.started", step=step, executable=cmd[0], timeout_sec=kwargs.get("timeout"))
    try:
        result = subprocess.run(cmd, cwd=ctx.job_dir, capture_output=True, text=True, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        _save_output(ctx, step, "stdout", getattr(error, "stdout", ""))
        _save_output(ctx, step, "stderr", getattr(error, "stderr", ""))
        message = (
            f"{step}: timeout after {error.timeout}s"
            if isinstance(error, subprocess.TimeoutExpired)
            else str(error)
        )
        ctx.emit(
            "process.failed",
            level="ERROR",
            step=step,
            duration_sec=round(time.perf_counter() - start, 3),
            error_type=type(error).__name__,
            error=message,
        )
        raise LLMError(message) from error
    _save_output(ctx, step, "stdout", result.stdout)
    _save_output(ctx, step, "stderr", result.stderr)
    ctx.emit(
        "process.finished",
        level="INFO" if result.returncode == 0 else "ERROR",
        step=step,
        duration_sec=round(time.perf_counter() - start, 3),
        returncode=result.returncode,
        stdout_path=f"{step}.stdout.txt",
        stderr_path=f"{step}.stderr.txt",
    )
    if result.returncode:
        raise LLMError(f"{step}: exit {result.returncode}: {result.stderr[-2000:]}")
    return result


def run_agy(step, prompt, schema, model, ctx):
    config = ctx.settings.agy
    cmd = [
        config["bin"],
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        str(ctx.settings.schemas_dir / schema),
        "--model",
        model,
        "--print-timeout",
        f"{config['timeout_sec']}s",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
        "--log-file",
        str(ctx.job_dir / f"{step}.agy.log"),
    ]
    result = _execute(cmd, ctx, step, timeout=config["timeout_sec"] + 60)
    output = json.loads(result.stdout)
    if output.get("status") != "SUCCESS" or output.get("structured_output") is None:
        raise LLMError(f"agy {step}: {output.get('status')}: {output.get('response', '')[-2000:]}")
    ctx.emit(
        "llm.provider_completed",
        step=step,
        provider="agy",
        model=model,
        conversation_id=output.get("conversation_id"),
        duration_sec=output.get("duration_seconds"),
        usage=output.get("usage"),
    )
    return output["structured_output"]


def run_codex(step, prompt, schema, ctx):
    config = ctx.settings.codex
    output = ctx.job_dir / f"{step}.response.json"
    output.unlink(missing_ok=True)
    cmd = [
        config["bin"],
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--model",
        config["model"],
        "--config",
        f'model_reasoning_effort="{config["reasoning_effort"]}"',
        "--config",
        'web_search="disabled"',
        "--output-schema",
        str(ctx.settings.schemas_dir / schema),
        "--output-last-message",
        str(output),
        "-",
    ]
    _execute(cmd, ctx, step, input=prompt, timeout=config["timeout_sec"])
    if not output.exists():
        raise LLMError(f"codex {step}: 출력 파일 없음")
    return json.loads(output.read_text(encoding="utf-8"))


def call_with_retry(call: Callable, prompt, validate=lambda output: output, on_retry=None):
    for attempt in range(2):
        try:
            return validate(call(prompt))
        except PipelineError:
            raise
        except (
            LLMError,
            json.JSONDecodeError,
            jsonschema.ValidationError,
            ValidationError,
            PydanticValidationError,
        ) as error:
            if attempt:
                raise
            if on_retry:
                on_retry(error)
            prompt += (
                "\n\n[이전 출력의 문제]\n"
                + str(error)[:3000]
                + "\n위 문제를 모두 고쳐 같은 형식으로 다시 출력하라."
            )


def invoke(ctx, step, provider, template, schema, values, validate=lambda output: output, model=None):
    prompt = render((ctx.settings.prompts_dir / template).read_text(encoding="utf-8"), **values)
    prompt = (
        "[실행 제약] 카탈로그 DB에 직접 접근하거나 소스 파일을 수정하지 말라. 지정한 출력 JSON만 반환하라.\n\n"
        + prompt
    )
    if provider == "agy" and step != "collect_page":
        prompt = (
            "[실행 제약] Stage 1의 브라우저 수집은 이미 완료되었다. aside MCP와 모든 브라우저 조작은 사용하지 말고 웹 검색 도구와 제공된 데이터만 사용하라.\n\n"
            + prompt
        )
    if step == "sales_research":
        prompt = (
            "[조사 범위] 판매처 최대 수는 상한이며 채워야 할 목표 수가 아니다. 확인된 판매처만 반환하라. 접근 실패한 사이트를 반복 탐색하지 말고 미확인 판매처는 제외하라.\n\n"
            + prompt
        )
    spec = json.loads((ctx.settings.schemas_dir / schema).read_text(encoding="utf-8"))
    attempts = 0

    def call(text):
        nonlocal attempts
        suffix = step if attempts == 0 else step + ".retry"
        attempts += 1
        (ctx.job_dir / f"{suffix}.request.txt").write_text(text, encoding="utf-8")
        ctx.emit(
            "llm.request",
            step=suffix,
            provider=provider,
            model=model if provider == "agy" else ctx.settings.codex["model"],
            schema=schema,
            attempt=attempts,
            request_path=f"{suffix}.request.txt",
        )
        try:
            output = (
                run_agy(suffix, text, schema, model, ctx)
                if provider == "agy"
                else run_codex(suffix, text, schema, ctx)
            )
        except Exception as error:
            ctx.emit(
                "llm.call_failed",
                level="ERROR",
                step=suffix,
                provider=provider,
                error_type=type(error).__name__,
                error=str(error)[:3000],
            )
            raise
        ctx.write_json(f"{suffix}.response.json", output)
        return output

    def check(output):
        try:
            jsonschema.validate(output, spec)
            validated = validate(OUTPUTS[schema].model_validate(output).model_dump())
        except Exception as error:
            ctx.emit(
                "llm.validation_failed",
                level="WARNING",
                step=step,
                attempt=attempts,
                error_type=type(error).__name__,
                error=str(error)[:3000],
            )
            raise
        ctx.emit("llm.validated", step=step, attempt=attempts, schema=schema)
        return validated

    return call_with_retry(
        call,
        prompt,
        check,
        lambda error: ctx.emit(
            "llm.retry",
            level="WARNING",
            step=step,
            next_attempt=2,
            error_type=type(error).__name__,
            error=str(error)[:3000],
        ),
    )
