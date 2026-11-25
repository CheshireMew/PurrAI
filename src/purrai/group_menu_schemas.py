"""Editable Telegram reply keyboard and its existing feature bindings."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from purrai.command_text import normalized_command


def menu_key(value):
    return normalized_command(value)


class MenuButton(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=64)
    action: Literal["signin", "balance", "lottery"]
    style: Literal["default", "primary", "success", "danger"] = "default"

    @field_validator("text")
    @classmethod
    def valid_text(cls, value):
        if (not menu_key(value) or menu_key(value).startswith("/")
                or any(character in value for character in "\r\n\t")
                or len(value.encode("utf-16-le")) // 2 > 64):
            raise ValueError("按钮文字须为单行的 1–64 个字符，不能以斜杠开头")
        return value


class MenuConfig(BaseModel):
    rows: list[list[MenuButton]] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def valid_rows(self):
        buttons = [button for row in self.rows for button in row]
        if any(not row or len(row) > 8 for row in self.rows) or len(buttons) > 32:
            raise ValueError("每行需要 1–8 个按钮，整个菜单最多 32 个按钮")
        if len({menu_key(button.text) for button in buttons}) != len(buttons):
            raise ValueError("按钮文字不能重复（忽略大小写和全半角差别）")
        return self


class MenuSave(BaseModel):
    config: MenuConfig
    version: int = Field(strict=True, ge=0)


class MenuOperation(BaseModel):
    operation: Literal["publish", "clear"]
    version: int = Field(strict=True, ge=0)
