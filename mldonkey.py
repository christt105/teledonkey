"""Minimal async client for the mldonkey console (telnet) interface.

mldonkey exposes a line-oriented command console on its telnet port (4000 inside
the container, mapped to 4002 on the host in this setup). We open a short-lived
connection per command (or per small sequence of commands), optionally
authenticate, run the command(s) and read each reply until the stream goes idle.

A few commands need more than one round-trip on the *same* connection:

* ``cancel`` first prints the file summary and asks ``Type 'confirm yes/no'``.
  The confirmation only counts inside the same telnet session, so we cannot send
  it as a separate ``run()`` call — we keep the socket open and answer there.
* a search submits the query with ``s``, the results trickle in from the network
  over a few seconds, and only then does ``vr`` list them.
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

    # --- Connection primitives ----------------------------------------------

    async def _connect(self):
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise MLDonkeyError(
                f"Cannot reach mldonkey at {self.host}:{self.port} ({exc})"
            ) from exc

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

    async def _auth(self, reader, writer) -> None:
        if self.password:
            writer.write(f"auth {self.user} {self.password}\n".encode())
            await writer.drain()
            await self._drain(reader, 0.5)

    async def _send(self, reader, writer, command: str, idle: float = 1.5) -> str:
        """Send one command on an open connection and return its cleaned reply."""
        writer.write(f"{command}\n".encode())
        await writer.drain()
        raw = await self._drain(reader, idle)
        return self._clean(raw, command)

    @staticmethod
    async def _close(writer) -> None:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def run(self, command: str) -> str:
        """Open a connection, optionally auth, run one command, return the reply."""
        reader, writer = await self._connect()
        try:
            # Swallow the welcome banner / initial prompt.
            await self._drain(reader, 1.0)
            await self._auth(reader, writer)
            return await self._send(reader, writer, command)
        finally:
            await self._close(writer)

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
        """Cancel a download, answering mldonkey's interactive confirmation.

        ``cancel <num>`` prints the file summary and asks for ``confirm yes`` on
        the *same* session, so we keep the socket open and answer there instead
        of issuing a second, independent command.
        """
        reader, writer = await self._connect()
        try:
            await self._drain(reader, 1.0)
            await self._auth(reader, writer)
            first = await self._send(reader, writer, f"cancel {num}")
            if re.search(r"confirm", first, re.IGNORECASE):
                second = await self._send(reader, writer, "confirm yes")
                return "\n".join(p for p in (first, second) if p).strip()
            return first
        finally:
            await self._close(writer)

    async def pause(self, num: int) -> str:
        return await self.run(f"pause {num}")

    async def resume(self, num: int) -> str:
        return await self.run(f"resume {num}")

    async def bandwidth(self) -> str:
        # Recent transfer rates.
        return await self.run("bw_stats")

    async def search(self, query: str, wait: float = 7.0) -> str:
        """Submit a network search and return the `vr` listing of its results.

        Results arrive asynchronously from servers/Kademlia, so we submit the
        query with ``s``, give the network ``wait`` seconds to answer, then read
        the results with ``vr`` — all on the same connection.
        """
        reader, writer = await self._connect()
        try:
            await self._drain(reader, 1.0)
            await self._auth(reader, writer)
            await self._send(reader, writer, f"s {query}")  # submit, ignore the ack
            await asyncio.sleep(wait)
            return await self._send(reader, writer, "vr", idle=2.5)
        finally:
            await self._close(writer)

    async def download_result(self, num: int) -> str:
        """Download a search result by its mldonkey result number (`d <num>`)."""
        return await self.run(f"d {num}")
