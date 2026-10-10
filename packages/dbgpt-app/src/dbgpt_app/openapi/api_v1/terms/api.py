"""Shared term endpoints; all users use the same terminology library."""

from typing import List
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .store import TermStore

router = APIRouter(prefix="/v1/chat/terms", tags=["Shared terms"])


class TermSource(BaseModel):
    title: str = Field(default="", max_length=512)
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        if urlparse(value).scheme not in {"http", "https"}:
            raise ValueError("来源只允许 HTTP/HTTPS 链接。")
        return value


class TermValue(BaseModel):
    term: str = Field(min_length=1, max_length=255)
    definition: str = Field(min_length=1, max_length=4000)
    formula: str = Field(default="", max_length=4000)
    formula_confirmed: bool = False
    datasource: str = Field(default="", max_length=255)
    aliases: List[str] = Field(default_factory=list, max_length=20)
    sources: List[TermSource] = Field(default_factory=list, max_length=10)
    source_type: str = Field(default="user", pattern="^(user|model|business|history)$")

    @field_validator("term", "definition")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("名词和解释不能为空。")
        return value.strip()

    @field_validator("aliases")
    @classmethod
    def valid_aliases(cls, values):
        if any(not v.strip() or len(v) > 255 for v in values):
            raise ValueError("别名不能为空或超过255字符。")
        return list(dict.fromkeys(v.strip() for v in values))

    @model_validator(mode="after")
    def formula_state(self):
        if not self.formula.strip():
            self.formula_confirmed = False
        return self


@router.get("")
def list_terms(term: str = "", datasource: str = ""):
    return TermStore().find(term, datasource)


@router.post("")
def save_term(value: TermValue):
    return TermStore().save(value.model_dump())


@router.put("/{record_id}")
def update_term(record_id: str, value: TermValue):
    try:
        return TermStore().update(record_id, value.model_dump())
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.delete("/{record_id}")
def delete_term(record_id: str):
    try:
        TermStore().delete(record_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    return {"deleted": True}
