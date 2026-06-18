import csv
import json
import os
from pathlib import Path
from typing import List, Tuple, Any
from dataclasses import dataclass

from .models import PrintTask, CounterType


@dataclass
class ImportResult:
    success: List[PrintTask]
    skipped: List[Tuple[int, str, str]]
    errors: List[Tuple[int, str, str]]


class TaskImporter:
    VALID_EXTENSIONS = {".csv", ".json"}

    COUNTER_MAP = {
        "A": CounterType.A,
        "A类": CounterType.A,
        "A类柜台": CounterType.A,
        "a": CounterType.A,
        "B": CounterType.B,
        "B类": CounterType.B,
        "B类柜台": CounterType.B,
        "b": CounterType.B,
        "C": CounterType.C,
        "C类": CounterType.C,
        "C类柜台": CounterType.C,
        "c": CounterType.C,
        "D": CounterType.D,
        "D类": CounterType.D,
        "D类柜台": CounterType.D,
        "d": CounterType.D,
    }

    @classmethod
    def import_file(cls, file_path: str, default_max_retries: int = 3) -> ImportResult:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = path.suffix.lower()
        if ext not in cls.VALID_EXTENSIONS:
            raise ValueError(f"不支持的文件格式: {ext}。请使用 CSV 或 JSON 文件。")

        if ext == ".csv":
            return cls._import_csv(path, default_max_retries)
        else:
            return cls._import_json(path, default_max_retries)

    @classmethod
    def _import_csv(cls, path: Path, default_max_retries: int) -> ImportResult:
        success: List[PrintTask] = []
        skipped: List[Tuple[int, str, str]] = []
        errors: List[Tuple[int, str, str]] = []

        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            required = {"filename", "copies", "counter"}
            if not required.issubset({fn.lower().strip() for fn in fieldnames}):
                missing = required - {fn.lower().strip() for fn in fieldnames}
                raise ValueError(f"CSV缺少必要列: {', '.join(missing)}。必需列: filename, copies, counter")

            col_map = {fn.lower().strip(): fn for fn in fieldnames}

            for row_num, row in enumerate(reader, start=2):
                raw = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
                filename = raw.get("filename", "")
                copies_str = raw.get("copies", "")
                counter_str = raw.get("counter", "")
                max_retries_str = raw.get("max_retries", "")
                priority_str = raw.get("priority", "")

                line_id = f"第{row_num}行"
                row_identifier = f"{filename or '空文件名'} | {copies_str or '空份数'} | {counter_str or '空柜台'}"

                validation_error = cls._validate_fields(filename, copies_str, counter_str)
                if validation_error:
                    errors.append((row_num, row_identifier, validation_error))
                    continue

                try:
                    copies = int(copies_str)
                except ValueError:
                    errors.append((row_num, row_identifier, f"份数格式错误: '{copies_str}' 不是有效整数"))
                    continue

                copies_error = cls._validate_copies(copies)
                if copies_error:
                    errors.append((row_num, row_identifier, copies_error))
                    continue

                counter = cls._parse_counter(counter_str)
                if counter is None:
                    errors.append((row_num, row_identifier, f"未知柜台类型: '{counter_str}'"))
                    continue

                max_retries = default_max_retries
                if max_retries_str:
                    try:
                        mr = int(max_retries_str)
                        if 0 <= mr <= 20:
                            max_retries = mr
                        else:
                            skipped.append((row_num, row_identifier,
                                            f"重试次数{mr}超出范围(0-20)，使用默认值{default_max_retries}"))
                    except ValueError:
                        skipped.append((row_num, row_identifier,
                                        f"重试次数字段格式错误，使用默认值{default_max_retries}"))

                priority_override = None
                if priority_str:
                    try:
                        p = int(priority_str)
                        if 1 <= p <= 999:
                            priority_override = p
                        else:
                            skipped.append((row_num, row_identifier,
                                            f"优先级{p}超出范围(1-999)，忽略自定义优先级"))
                    except ValueError:
                        skipped.append((row_num, row_identifier, "优先级字段格式错误，忽略自定义优先级"))

                task = PrintTask.create(
                    filename=filename,
                    copies=copies,
                    counter=counter,
                    max_retries=max_retries,
                    priority_override=priority_override,
                )
                success.append(task)

        return ImportResult(success=success, skipped=skipped, errors=errors)

    @classmethod
    def _import_json(cls, path: Path, default_max_retries: int) -> ImportResult:
        success: List[PrintTask] = []
        skipped: List[Tuple[int, str, str]] = []
        errors: List[Tuple[int, str, str]] = []

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            raise ValueError("JSON文件根节点必须是数组")

        for idx, item in enumerate(data):
            row_num = idx + 1
            if not isinstance(item, dict):
                errors.append((row_num, str(item), "条目不是对象类型"))
                continue

            filename = str(item.get("filename", "")).strip()
            copies_raw = item.get("copies", "")
            counter_str = str(item.get("counter", "")).strip()
            max_retries_raw = item.get("max_retries")
            priority_raw = item.get("priority")

            copies_str = str(copies_raw) if copies_raw is not None else ""
            row_identifier = f"{filename or '空文件名'} | {copies_str or '空份数'} | {counter_str or '空柜台'}"

            validation_error = cls._validate_fields(filename, copies_str, counter_str)
            if validation_error:
                errors.append((row_num, row_identifier, validation_error))
                continue

            try:
                copies = int(copies_str)
            except (ValueError, TypeError):
                errors.append((row_num, row_identifier, f"份数格式错误: '{copies_str}' 不是有效整数"))
                continue

            copies_error = cls._validate_copies(copies)
            if copies_error:
                errors.append((row_num, row_identifier, copies_error))
                continue

            counter = cls._parse_counter(counter_str)
            if counter is None:
                errors.append((row_num, row_identifier, f"未知柜台类型: '{counter_str}'"))
                continue

            max_retries = default_max_retries
            if max_retries_raw is not None:
                try:
                    mr = int(max_retries_raw)
                    if 0 <= mr <= 20:
                        max_retries = mr
                    else:
                        skipped.append((row_num, row_identifier,
                                        f"重试次数{mr}超出范围(0-20)，使用默认值{default_max_retries}"))
                except (ValueError, TypeError):
                    skipped.append((row_num, row_identifier,
                                    f"重试次数字段格式错误，使用默认值{default_max_retries}"))

            priority_override = None
            if priority_raw is not None:
                try:
                    p = int(priority_raw)
                    if 1 <= p <= 999:
                        priority_override = p
                    else:
                        skipped.append((row_num, row_identifier,
                                        f"优先级{p}超出范围(1-999)，忽略自定义优先级"))
                except (ValueError, TypeError):
                    skipped.append((row_num, row_identifier, "优先级字段格式错误，忽略自定义优先级"))

            task = PrintTask.create(
                filename=filename,
                copies=copies,
                counter=counter,
                max_retries=max_retries,
                priority_override=priority_override,
            )
            success.append(task)

        return ImportResult(success=success, skipped=skipped, errors=errors)

    @staticmethod
    def _validate_fields(filename: str, copies_str: str, counter_str: str) -> str | None:
        if not filename:
            return "文件名为空，缺少打印目标文件"
        if not copies_str:
            return "份数为空，缺少打印份数"
        if not counter_str:
            return "柜台类型为空，缺少所属柜台"
        return None

    @staticmethod
    def _validate_copies(copies: int) -> str | None:
        if copies <= 0:
            return f"份数非法: {copies}，必须为正整数"
        if copies > 9999:
            return f"份数非法: {copies}，单次打印不能超过9999份"
        return None

    @classmethod
    def _parse_counter(cls, s: str) -> CounterType | None:
        return cls.COUNTER_MAP.get(s.strip())
