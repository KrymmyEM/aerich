from __future__ import annotations

import contextlib
from typing import Any, Callable, TypedDict

from pydantic import BaseModel
from tortoise import BaseDBAsyncClient


class ColumnInfoDict(TypedDict):
    name: str
    pk: str
    index: str
    null: str
    default: str
    length: str
    comment: str


FieldMapDict = dict[str, Callable[..., str]]


class EnumDataType(BaseModel):
    row_name: str
    row_values: str
    class_name: str | None = None
    class_values: list[str] = []

    def get_enum_class(self) -> str:
        self.class_name = self.get_class_name()
        if len(self.class_values) != len(self.row_values.split(";")):
            self.row_values = self.row_values.strip("{")
            self.row_values = self.row_values.strip("}")
            row_values = self.row_values.split(";")
            for value in row_values:
                if value.isdigit():
                    continue
                name_value = re.sub(r"[\s,-]+", "_", value.strip('"')).replace("@", "").upper()
                self.class_values.append(f'    {name_value} = "{value}"')

        result = f"class {self.class_name}(str, Enum):\n"
        result += "\n".join(self.class_values)

        return result

    def get_class_name(self) -> str:
        if not self.class_name:
            class_name = self.row_name.replace("@", "").replace("_", " ").title().replace(" ", "")
            self.class_name = class_name + "Enum"
        return self.class_name

    def enum_type(self) -> dict:
        return {"enum_type": f"enum_type={self.get_class_name()}, "}


class Column(BaseModel):
    name: str
    data_type: str
    null: bool
    default: Any
    comment: str | None = None
    pk: bool
    unique: bool
    index: bool
    length: int | None = None
    extra: str | None = None
    decimal_places: int | None = None
    max_digits: int | None = None

    def translate(self) -> ColumnInfoDict:
        comment = default = length = index = null = pk = ""
        if self.pk:
            pk = "primary_key=True, "
        else:
            if self.unique:
                index = "unique=True, "
            elif self.index:
                index = "db_index=True, "
        if self.data_type in ("varchar", "VARCHAR"):
            length = f"max_length={self.length}, "
        elif self.data_type in ("decimal", "numeric"):
            length_parts = []
            if self.max_digits:
                length_parts.append(f"max_digits={self.max_digits}")
            if self.decimal_places:
                length_parts.append(f"decimal_places={self.decimal_places}")
            if length_parts:
                length = ", ".join(length_parts) + ", "
        if self.null:
            null = "null=True, "
        if self.default is not None and not self.pk:
            if self.data_type in ("tinyint", "INT"):
                default = f"default={'True' if self.default == '1' else 'False'}, "
            elif self.data_type == "bool":
                default = f"default={'True' if self.default == 'true' else 'False'}, "
            elif self.data_type in ("datetime", "timestamptz", "TIMESTAMP"):
                if self.default == "CURRENT_TIMESTAMP":
                    if self.extra == "DEFAULT_GENERATED on update CURRENT_TIMESTAMP":
                        default = "auto_now=True, "
                    else:
                        default = "auto_now_add=True, "
            else:
                if "::" in self.default:
                    default = f"default={self.default.split('::')[0]}, "
                elif self.default.endswith("()"):
                    default = ""
                elif self.default == "":
                    default = 'default=""'
                else:
                    default = f"default={self.default}, "

        if self.comment:
            comment = f"description='{self.comment}', "
        return {
            "name": self.name,
            "pk": pk,
            "index": index,
            "null": null,
            "default": default,
            "length": length,
            "comment": comment,
        }


class Inspect:
    _table_template = "class {table}(Model):\n"

    def __init__(self, conn: BaseDBAsyncClient, tables: list[str] | None = None) -> None:
        self.conn = conn
        with contextlib.suppress(AttributeError):
            self.database = conn.database  # type:ignore[attr-defined]
        self.tables = tables

    @property
    def field_map(self) -> FieldMapDict:
        raise NotImplementedError

    def get_field(self, table: str, column, enums_types: dict[str, EnumDataType]) -> str:
        enum_key = enums_types.get(column.data_type) or enums_types.get(f"{table}_{column.name}")
        if enum_key:
            return self.field_map["enum"](**enum_key.enum_type(), **column.translate())
        return self.field_map[column.data_type](**column.translate())

    async def inspect(self) -> str:
        if not self.tables:
            self.tables = await self.get_all_tables()

        imports: list[str] = ["from tortoise import Model, fields"]
        result_parts: list[str] = []

        enums_types: dict[str, EnumDataType] = await self.get_enums_data_types()
        enums: list[str] = []

        if enums_types:
            imports.append("from enum import Enum")
            enums = [value.get_enum_class() for value in enums_types.values()]

        for table in self.tables:
            columns = await self.get_columns(table)

            fields_lines: list[str] = []

            for column in columns:
                try:
                    trans_func = self.field_map[column.data_type]

                except KeyError as e:
                    if not self._special_fields or column.data_type not in self._special_fields:
                        raise NotSupportError(
                            f"Can't translate {column.data_type=} to be tortoise field"
                        ) from e

                    field_class = self._special_fields[column.data_type]
                    is_normal_field = True

                    if "." in field_class:
                        module, field_class = field_class.rsplit(".", 1)

                        if module != "fields":
                            imports.append(f"from {module} import {field_class}")
                            is_normal_field = False

                    trans_func = partial(
                        self.get_field_string,
                        field_class,
                        is_normal_field=is_normal_field,
                    )

                field_str = trans_func(**column.translate())
                fields_lines.append(f"    {field_str}")

            model_name = self._table_template.format(
                table=table.title().replace("_", "")
            )

            meta = (
                f"    class Meta:\n"
                f"        table = '{table}'\n"
            )

            model_block = (
                f"{model_name}\n"
                f"{'\n'.join(fields_lines)}\n\n"
                f"{meta}"
            )

            result_parts.append(model_block)

        header = "\n".join(dict.fromkeys(imports))  # remove duplicates, keep order

        return "\n\n\n".join([header, *enums, *result_parts])

    async def _get_enums(self):
        raise NotImplementedError

    async def get_enums_data_types(self) -> dict[str, EnumDataType]:
        raise NotImplementedError

    async def get_enums_names(self) -> set[str]:
        raise NotImplementedError

    async def get_columns(self, table: str) -> list[Column]:
        raise NotImplementedError

    async def get_all_tables(self) -> list[str]:
        raise NotImplementedError

    @staticmethod
    def get_field_string(
        field_class: str, arguments: str = "{null}{default}{comment}", **kwargs
    ) -> str:
        name: str = kwargs["name"]
        arguments += "{source_field}"
        kwargs["source_field"] = f"source_field='{name}'"
        if "-" in name:
            name = name.replace("-", "_")
        if name[0].isdigit():
            name = "_" + name
        name = name.replace("@", "")

        field_params = arguments.format(**kwargs).strip().rstrip(",")
        return f"{name} = fields.{field_class}({field_params})"

    @classmethod
    def decimal_field(cls, **kwargs) -> str:
        return cls.get_field_string("DecimalField", **kwargs)

    @classmethod
    def time_field(cls, **kwargs) -> str:
        return cls.get_field_string("TimeField", **kwargs)

    @classmethod
    def date_field(cls, **kwargs) -> str:
        return cls.get_field_string("DateField", **kwargs)

    @classmethod
    def float_field(cls, **kwargs) -> str:
        return cls.get_field_string("FloatField", **kwargs)

    @classmethod
    def datetime_field(cls, **kwargs) -> str:
        return cls.get_field_string("DatetimeField", **kwargs)

    @classmethod
    def text_field(cls, **kwargs) -> str:
        return cls.get_field_string("TextField", **kwargs)

    @classmethod
    def char_field(cls, **kwargs) -> str:
        arguments = "{pk}{index}{length}{null}{default}{comment}"
        return cls.get_field_string("CharField", arguments, **kwargs)

    @classmethod
    def int_field(cls, field_class="IntField", **kwargs) -> str:
        arguments = "{pk}{index}{default}{comment}"
        return cls.get_field_string(field_class, arguments, **kwargs)

    @classmethod
    def smallint_field(cls, **kwargs) -> str:
        return cls.int_field("SmallIntField", **kwargs)

    @classmethod
    def bigint_field(cls, **kwargs) -> str:
        return cls.int_field("BigIntField", **kwargs)

    @classmethod
    def bool_field(cls, **kwargs) -> str:
        return cls.get_field_string("BooleanField", **kwargs)

    @classmethod
    def uuid_field(cls, **kwargs) -> str:
        arguments = "{pk}{index}{default}{comment}"
        return cls.get_field_string("UUIDField", arguments, **kwargs)

    @classmethod
    def json_field(cls, **kwargs) -> str:
        return cls.get_field_string("JSONField", **kwargs)

    @classmethod
    def binary_field(cls, **kwargs) -> str:
        return cls.get_field_string("BinaryField", **kwargs)

    @classmethod
    def charenum_field(cls, **kwargs) -> str:
        arguments = "{enum_type}{null}"
        return cls.get_field_string("CharEnumField", arguments, **kwargs)