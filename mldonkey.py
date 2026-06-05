"""Minimal async client for the mldonkey console (telnet) interface.

mldonkey exposes a line-oriented command console on its telnet port (4000 inside
the container, mapped to 4002 on the host in this setup). We open a short-lived
connection per command, optionally authenticate, run the command and read the
reply until the stream goes idle.
"""

import asyncio
import re

# Matches ANSI / VT100 escape sequences that mldonkey sprinkles into its output.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# A trailing console prompt that we don't want to show to the user.
PROMPT_RE = re.compile(r"^\s*(MLdonkey command-line:|>)\s*$", re.IGNORECASE)


class MLDonkeyError(Exception):
    """Raised when we cannot reach or talk to mldonkey."""


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class MLDonkeyClient:
    def __init__(
        self,
        host: str,
        port: int,
        user: str = "admin",
        password: str = "",
        timeout: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout

    async def _drain(self, reader: asyncio.StreamReader, idle: float) -> str:
        """Read until no data arrives for `idle` seconds."""
        chunks: list[bytes] = []
        while True:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=idle)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def run(self, command: str) -> str:
        """Open a connection, optionally auth, run one command, return the reply."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise MLDonkeyError(
                f"Cannot reach mldonkey at {self.host}:{self.port} ({exc})"
            ) from exc

        try:
            # Swallow the welcome banner / initial prompt.
            await self._drain(reader, 1.0)

            if self.password:
                writer.write(f"auth {self.user} {self.password}\n".encode())
                await writer.drain()
                await self._drain(reader, 0.5)

            writer.write(f"{command}\n".encode())
            await writer.drain()
            raw = await self._drain(reader, 1.5)
            return self._clean(raw, command)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _clean(raw: str, command: str) -> str:
        text = strip_ansi(raw)
        lines = []
        for line in text.splitlines():
            # Strip a leading console prompt ("> ") that prefixes echoed input.
            line = re.sub(r"^\s*>\s?", "", line)
            stripped = line.strip()
            # Drop the echoed command and bare prompts.
            if stripped == command or PROMPT_RE.match(stripped):
                continue
            lines.append(line.rstrip())
        # Collapse leading/trailing blank lines.
        return "\n".join(lines).strip("\n")

    # --- High level helpers -------------------------------------------------

    async def add_link(self, url: str) -> str:
        return await self.run(f"dllink {url}")

    async def view_downloads(self) -> str:
        return await self.run("vd")

    async def cancel(self, num: int) -> str:
        return await self.run(f"cancel {num}")

    async def pause(self, num: int) -> str:
        return await self.run(f"pause {num}")

    async def resume(self, num: int) -> str:
        return await self.run(f"resume {num}")

    async def bandwidth(self) -> str:
        # Recent transfer rates.
        return await self.run("bw_stats")
