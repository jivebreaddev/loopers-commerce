from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_usage: StrictBool = True


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=256)
    messages: list[Message] = Field(min_length=1, max_length=128)
    max_tokens: StrictInt = Field(default=256, ge=1)
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=1, gt=0, le=1)
    stream: StrictBool = False
    stream_options: StreamOptions | None = None
    n: Literal[1] = 1

    @model_validator(mode="after")
    def bound_prompt_size(self):
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if sum(len(m.content.encode("utf-8")) for m in self.messages) > 131072:
            raise ValueError("Messages must fit within 128 KiB")
        return self
