"""html_interpreter tool — render HTML as an interactive report."""

import json
import logging
import os
import re
from html import unescape
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

from dbgpt.agent.resource.tool.base import tool
from .query_quality import _ratio_definitions, ratio_delivery_hint
from .report_rendering import render_results

logger = logging.getLogger(__name__)


def make_html_interpreter(react_state: Dict[str, Any], skills_dir: str):
    @tool(
        description=(
            "将 HTML 渲染为可交互的网页报告，这是向用户展示网页报告的唯一方式。"
            "传入完整报告；若检查失败，请修正缺失项后重新调用。"
            "报告生成成功且全部目标达成后才可 terminate。"
            "当前任务要求计算比率的财务报告必须使用result_ids引用sql_query/code_interpreter返回的已验证结果。"
            '调用 {"result_ids":["实际result_id"],"title":"报告标题"}，系统渲染数据与事实。'
            "此模式不得传入自由HTML、模板或文件补写数字；缺失时同时选择真实销售缺失明细结果。"
            "【仅非财务报告使用以下通用模式】直接传入完整的 HTML 字符串："
            '{"html": "<html>...</html>", "title": "报告标题"}。'
            "你需要自己生成完整的 HTML 代码"
            "（包含 <!DOCTYPE html>、<html>、<head>、<body> 等），"
            "然后传给 html 参数即可。"
            "HTML 可以很长，没有长度限制，不需要分段传入；"
            "若报告含多部分内容，请合并进【同一份】HTML 一次性渲染，"
            "不要分多次生成多份报告。"
            "【禁止】不要用 code_interpreter 写 HTML 再 print，"
            "不要用 code_interpreter 把 HTML 写入文件再读取，"
            "直接把 HTML 传给本工具即可。"
            "【技能模式 - 仅在使用技能时可选】"
            "如果正在使用技能（skill），可以用模板模式："
            '{"template_path": "技能名/templates/模板.html", '
            '"data": {"KEY": "值"}, "title": "标题"}。'
            '也可以用文件模式：{"file_path": "/path/to/report.html"}'
        )
    )
    async def html_interpreter(
        html: str = "",
        title: str = "Report",
        file_path: str = "",
        template_path: str = "",
        data: dict | str = None,
        result_ids: List[str] = None,
    ) -> str:
        """Render HTML as an interactive web report."""
        from dbgpt.configs.model_config import STATIC_MESSAGE_IMG_PATH

        react_state.pop("rendered_report", None)
        skills_path = Path(skills_dir).expanduser().resolve()
        factual_summary = None
        if _ratio_definitions(react_state) or result_ids is not None:
            try:
                if html or file_path or template_path or data:
                    raise ValueError("财务报告只能通过result_ids生成，不能传入自由HTML或模板事实。")
                html, factual_summary = render_results(react_state, result_ids, title)
            except (ValueError, TypeError, KeyError) as error:
                available = {rid: result["columns"] for rid, result in react_state.get("validated_results", {}).items()}
                feedback = (str(error) + "\n当前任务可用result_ids及列：" + json.dumps(available, ensure_ascii=False)
                            + "\n财务模式仅传result_ids和title，html保持空；需要同一范围的完整比率结果和真实缺失明细。"
                            + ("\n" + ratio_delivery_hint(react_state) if ratio_delivery_hint(react_state) else ""))
                return json.dumps({"success": False, "error": feedback, "chunks": [
                    {"output_type": "text", "content": feedback},
                ]}, ensure_ascii=False)

        # ── Mode 1: template_path + data ──
        if template_path and template_path.strip():
            tp = template_path.strip()
            target = (skills_path / tp).resolve()
            try:
                target.relative_to(skills_path)
            except ValueError:
                return json.dumps(
                    {
                        "success": False,
                        "chunks": [
                            {
                                "output_type": "text",
                                "content": f"Invalid template_path: {tp}",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            if not target.is_file():
                return json.dumps(
                    {
                        "success": False,
                        "chunks": [
                            {
                                "output_type": "text",
                                "content": (
                                    f"Template not found: {tp}. "
                                    "Please retry using the `html` parameter "
                                    "directly — "
                                    "generate complete HTML yourself and pass it via "
                                    '{"html": "<html>...</html>", "title": "title"}.'
                                ),
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            try:
                raw_template = target.read_text(encoding="utf-8")
            except Exception as e:
                return json.dumps(
                    {
                        "success": False,
                        "chunks": [
                            {
                                "output_type": "text",
                                "content": f"Error reading template: {e}",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )

            replacements = data
            if isinstance(replacements, str):
                try:
                    replacements = json.loads(replacements)
                except Exception:
                    try:
                        fixed = str(replacements).rstrip()
                        if not fixed.endswith("}"):
                            fixed += '"}' if not fixed.endswith('"') else "}"
                        replacements = json.loads(fixed)
                    except Exception:
                        replacements = {}
            if not isinstance(replacements, dict):
                replacements = {}

            auto_data = react_state.get("auto_data", {})
            if isinstance(auto_data, dict):
                replacements = {**auto_data, **replacements}

            ratio_data = react_state.get("ratio_data", {})
            if isinstance(ratio_data, dict):
                replacements = {**ratio_data, **replacements}

            image_url_map = react_state.get("image_url_map", {})
            if isinstance(image_url_map, dict):
                for stem, url in image_url_map.items():
                    chart_key = f"CHART_{stem.upper()}"
                    if chart_key not in replacements:
                        replacements[chart_key] = url

            def _replace_placeholder(m):
                key = m.group(1)
                return str(replacements.get(key, ""))

            html = re.sub(r"\{\{([A-Z_0-9]+)\}\}", _replace_placeholder, raw_template)
            if not title or title == "Report":
                title = target.stem

        # ── Mode 2: file_path ──
        elif file_path and file_path.strip():
            fp = file_path.strip()
            if not os.path.isfile(fp):
                cid = react_state.get("conv_id") or "default"
                from dbgpt.configs.model_config import PILOT_PATH

                alt = os.path.join(PILOT_PATH, "data", cid, os.path.basename(fp))
                if os.path.isfile(alt):
                    fp = alt
                else:
                    return json.dumps(
                        {
                            "success": False,
                            "chunks": [
                                {
                                    "output_type": "text",
                                    "content": f"File not found: {file_path}",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    html = f.read()
                if not title or title == "Report":
                    title = os.path.splitext(os.path.basename(fp))[0]
            except Exception as e:
                return json.dumps(
                    {
                        "success": False,
                        "chunks": [
                            {
                                "output_type": "text",
                                "content": f"Error reading file: {e}",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )

        # ── Mode 3: inline html ──
        if html and isinstance(html, str) and not template_path and not file_path:
            if "\\n" in html:
                html = html.replace("\\n", "\n")
            if "\\t" in html:
                html = html.replace("\\t", "\t")

        if not html or not html.strip():
            return json.dumps(
                {
                    "success": False,
                    "chunks": [
                        {"output_type": "text", "content": "No HTML content provided"}
                    ],
                },
                ensure_ascii=False,
            )

        # Post-process: fix image URLs
        fixed_html = html.strip()
        try:
            IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
            name_to_served: Dict[str, str] = {}
            if os.path.isdir(STATIC_MESSAGE_IMG_PATH):
                for fname in os.listdir(STATIC_MESSAGE_IMG_PATH):
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in IMAGE_EXTS:
                        continue
                    m = re.match(r"^[0-9a-f]{8}_(.+)$", fname, re.IGNORECASE)
                    if m:
                        name_to_served[m.group(1).lower()] = f"/images/{fname}"

            if name_to_served:

                def _fix_img_src(match: re.Match) -> str:
                    prefix = match.group(1)
                    raw_path = match.group(2)
                    quote = match.group(3)
                    filename = raw_path.rsplit("/", 1)[-1].lower()
                    if re.match(r"^[0-9a-f]{8}_.+$", filename, re.IGNORECASE):
                        return match.group(0)
                    if filename in name_to_served:
                        return f"{prefix}{name_to_served[filename]}{quote}"
                    return match.group(0)

                fixed_html = re.sub(
                    r"""(src\s*=\s*["'])([^"']+\.(?:png|jpg|jpeg|gif|svg|webp))(["'])""",
                    _fix_img_src,
                    fixed_html,
                    flags=re.IGNORECASE,
                )
        except Exception:
            pass

        # Auto-append missing images
        try:
            gen_images = react_state.get("generated_images", [])
            if gen_images and factual_summary is None:
                html_img_stems = set(
                    re.sub(r"^[0-9a-f]+_", "", os.path.basename(src))
                    for src in re.findall(
                        r'<img[^>]+src=["\']([^"\']+)["\']', fixed_html, re.IGNORECASE
                    )
                )

                def _img_stem(url):
                    return re.sub(r"^[0-9a-f]+_", "", os.path.basename(url))

                missing = [
                    url
                    for url in gen_images
                    if url not in fixed_html and _img_stem(url) not in html_img_stems
                ]
                if missing:
                    imgs_html = "".join(
                        f'<div style="margin:16px 0">'
                        f'<img src="{url}" '
                        f'style="max-width:100%;height:auto;border-radius:8px">'
                        f"</div>"
                        for url in missing
                    )
                    section = (
                        '<div style="margin-top:32px"><h2>📊 分析图表</h2>'
                        f"{imgs_html}</div>"
                    )
                    if "</body>" in fixed_html.lower():
                        fixed_html = re.sub(
                            r"(</body>)",
                            section + r"\1",
                            fixed_html,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                    else:
                        fixed_html += section
        except Exception:
            pass

        missing: List[str] = []
        if not all(
            tag in fixed_html.casefold()
            for tag in ("<html", "</html>", "<body", "</body>")
        ):
            missing.append("HTML 必须包含完整的 html 和 body 标签")
        if react_state.get("requires_database_profile"):
            profile = react_state.get("database_profile")
            if not profile:
                missing.append("先调用 database_profile 获取真实数据库画像")
            else:
                # Data may be in a table or in the script powering interactive views.
                normalized = re.sub(r"[,，\s]", "", unescape(fixed_html)).casefold()
                for table in profile["tables"]:
                    required = [table["name"]]
                    required.extend(c["name"] for c in table["columns"])
                    required.extend(c["type"] for c in table["columns"])
                    count_present = re.search(
                        rf"(?<!\d){table['row_count']}(?!\d)", normalized
                    )
                    if not count_present or any(
                        re.sub(r"[,，\s]", "", item).casefold() not in normalized
                        for item in required
                    ):
                        missing.append(f"报告缺少 {table['name']} 的字段、类型或行数")

        from .sql_query import _save_result

        # Save HTML separately from SQL data, within the existing session-file root.
        report_path = _save_result(react_state, fixed_html, "report", suffix=".html")
        relative_path = f"{react_state.get('conv_id') or 'default'}/{report_path.name}"
        report_url = "/api/v1/agent/files/download?file_path=" + quote(relative_path)
        react_state["rendered_report"] = {
            "path": str(report_path),
            "url": report_url,
            "missing": missing,
            **({"summary": factual_summary, "result_ids": result_ids} if factual_summary else {}),
        }
        message = (
            ("报告交付检查未通过：" + "；".join(missing))
            if missing
            else ("HTML 报告已生成并发送到预览面板。全部目标达成后可 terminate。")
        )
        chunks: List[Dict[str, Any]] = [
            {
                "output_type": "text",
                "content": message + f"\n[查看报告]({report_url})",
            },
            {"output_type": "html", "content": fixed_html, "title": title},
        ]
        return json.dumps(
            {
                "success": not missing,
                "error": message if missing else None,
                "chunks": chunks,
            },
            ensure_ascii=False,
        )

    return html_interpreter
