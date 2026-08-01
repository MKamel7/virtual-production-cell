"""The OPC UA interface, including a real client connecting under security.

The acceptance criterion for this project says an OPC UA client reads live data.
A test that checked the address space through the server's own objects would not
show that: it would show the server agreeing with itself. So this file starts
the server and connects to it with `asyncua`'s client over an encrypted channel
with a username, which is the same path a supervisor or UaExpert takes.

The security half is the part worth having. Almost every OPC UA demonstration
runs `SecurityPolicy None` with anonymous login, which is a plaintext
unauthenticated channel wearing the badge of a standard that has real security
in it. There is a test below that anonymous is refused, because a security
setting nobody has watched reject anything is an assumption.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from vpc.opcua import (
    APPLICATION_URI,
    NAMESPACE,
    Credentials,
    TagGroup,
    build_address_space,
    check_names_agree,
    ensure_certificate,
    make_server,
    publish,
    tag_values,
)
from vpc.packml import State
from vpc.packtags import PackTags, StopReason, UnitMode

T = TypeVar("T")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run(coroutine: Awaitable[T]) -> T:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


# --- the pure half, which is most of the interesting part --------------------
def test_the_address_space_and_the_tag_structure_name_the_same_things() -> None:
    """Two copies of a name list is one copy plus a future defect.

    A supervisor that connects and finds a name missing reports nothing useful,
    and the fault looks like a network problem for the first hour.
    """
    check_names_agree()


def test_status_reports_the_numeric_state_code_not_the_name() -> None:
    """PackTags puts an integer on the wire, and a supervisor decodes it.

    Publishing the string would be friendlier to read and wrong: every other
    machine on the line publishes the number.
    """
    tags = PackTags()
    tags.update(State.EXECUTE, StopReason.NONE, produced=0, rejected=0,
                blocked=False, starved=False, speed=100)

    assert tag_values(tags)[TagGroup.STATUS]["StateCurrent"] == 6


def test_admin_counts_scrap_separately_from_throughput() -> None:
    tags = PackTags()
    tags.update(State.EXECUTE, StopReason.NONE, produced=90, rejected=10,
                blocked=False, starved=False, speed=100)

    admin = tag_values(tags)[TagGroup.ADMIN]
    assert admin["ProdProcessedCount"] == 100, "processed must include scrap"
    assert admin["ProdDefectiveCount"] == 10


def test_a_certificate_is_generated_with_the_application_uri_in_it(
        tmp_path: Path) -> None:
    """The URI must match or clients refuse the connection.

    They report it as a trust failure rather than a mismatch, which sends people
    looking in the wrong place. This is the cheapest possible guard against
    that afternoon.
    """
    from cryptography import x509

    cert, key = ensure_certificate(tmp_path / "c.der", tmp_path / "k.pem")
    parsed = x509.load_der_x509_certificate(cert.read_bytes())
    names = parsed.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value

    assert APPLICATION_URI in names.get_values_for_type(
        x509.UniformResourceIdentifier)
    assert key.read_bytes().startswith(b"-----BEGIN")


def test_an_existing_certificate_is_not_regenerated(tmp_path: Path) -> None:
    """Regenerating on every start would change identity under connected
    clients, which presents as an intermittent trust failure."""
    cert, key = ensure_certificate(tmp_path / "c.der", tmp_path / "k.pem")
    first = cert.read_bytes()

    ensure_certificate(tmp_path / "c.der", tmp_path / "k.pem")

    assert cert.read_bytes() == first


# --- a real client, over an encrypted channel --------------------------------
async def with_server(port: int,
                      body: Callable[[dict[str, Any], PackTags], Awaitable[T]],
                      ) -> T:
    endpoint = f"opc.tcp://127.0.0.1:{port}/vpc/"
    server = await make_server(endpoint)
    tags = PackTags()
    nodes = await build_address_space(server, tags)
    async with server:
        await publish(nodes, tags)
        return await body(nodes, tags)


async def connect(port: int, tmp_path: Path,
                  credentials: Credentials | None = None) -> Any:
    """A client configured the way a supervisor would be.

    Sign and encrypt needs a certificate at BOTH ends: the secure channel is
    mutual, and a client without one cannot open it whatever its username is.
    """
    from asyncua import Client

    account = credentials or Credentials()
    cert, key = ensure_certificate(tmp_path / "client.der",
                                   tmp_path / "client-key.pem",
                                   common_name="test-client",
                                   uri="urn:test:client")
    server_cert, _ = ensure_certificate()
    client = Client(f"opc.tcp://127.0.0.1:{port}/vpc/")
    client.application_uri = "urn:test:client"
    await client.set_security_string(
        f"Basic256Sha256,SignAndEncrypt,{cert},{key},{server_cert}")
    client.set_user(account.username)
    client.set_password(account.password)
    return client


def test_a_real_client_reads_live_data_over_an_encrypted_channel(
        tmp_path: Path) -> None:
    """The acceptance criterion, demonstrated rather than asserted.

    Same path UaExpert takes: encrypted channel, username, browse to the tag,
    read the value the machine published.
    """
    port = free_port()

    async def body(nodes: dict[str, Any], tags: PackTags) -> tuple[int, int]:
        tags.update(State.EXECUTE, StopReason.NONE, produced=42, rejected=6,
                    blocked=False, starved=True, speed=100)
        await publish(nodes, tags)

        client = await connect(port, tmp_path)
        async with client:
            # The index is looked up rather than assumed. A namespace index is
            # assigned by the server at registration and is not stable across
            # servers, so hard coding it is how a supervisor works against one
            # machine and silently browses nothing on the next.
            ns = await client.get_namespace_index(NAMESPACE)
            cell = await client.nodes.objects.get_child([f"{ns}:Cell"])
            status = await cell.get_child([f"{ns}:Status", f"{ns}:StateCurrent"])
            processed = await cell.get_child(
                [f"{ns}:Admin", f"{ns}:ProdProcessedCount"])
            starved = await cell.get_child(
                [f"{ns}:Status", f"{ns}:EquipmentStarved"])
            assert await starved.read_value() is True
            return await status.read_value(), await processed.read_value()

    state, processed = run(with_server(port, body))

    assert state == 6, "the client did not read Execute"
    assert processed == 48, "the client did not read the live counters"


def test_anonymous_access_is_refused(tmp_path: Path) -> None:
    """A security setting nobody has watched reject anything is an assumption.

    This is the half of an OPC UA story that usually is not there, and its
    absence is invisible: an anonymous server browses beautifully.
    """
    from asyncua import Client

    port = free_port()

    async def body(nodes: dict[str, Any], tags: PackTags) -> None:
        client = Client(f"opc.tcp://127.0.0.1:{port}/vpc/")
        with pytest.raises(Exception):     # noqa: B017 - any refusal will do
            async with client:
                await client.nodes.objects.get_children()

    run(with_server(port, body))


def test_a_wrong_password_is_refused(tmp_path: Path) -> None:
    """The control case. Without it the test above passes on a server that
    refuses everybody, including the supervisor."""
    port = free_port()

    async def body(nodes: dict[str, Any], tags: PackTags) -> None:
        client = await connect(port, tmp_path,
                               Credentials(username="supervisor",
                                           password="not-the-password"))
        with pytest.raises(Exception):     # noqa: B017
            async with client:
                await client.nodes.objects.get_children()

    run(with_server(port, body))


def test_command_tags_are_writable_and_status_tags_are_not(
        tmp_path: Path) -> None:
    """Direction of ownership, which is the classic PackTags mistake.

    A supervisor able to write Status could overwrite the machine's own view of
    itself, at which point two things own one value.
    """
    port = free_port()

    async def body(nodes: dict[str, Any], tags: PackTags) -> tuple[bool, bool]:
        from asyncua import ua

        client = await connect(port, tmp_path)
        async with client:
            ns = await client.get_namespace_index(NAMESPACE)
            cell = await client.nodes.objects.get_child([f"{ns}:Cell"])
            command = await cell.get_child([f"{ns}:Command", f"{ns}:MachSpeed"])
            status = await cell.get_child([f"{ns}:Status", f"{ns}:StateCurrent"])

            command_writable = bool(
                (await command.read_attribute(ua.AttributeIds.AccessLevel))
                .Value.Value & ua.AccessLevel.CurrentWrite.mask)
            status_writable = bool(
                (await status.read_attribute(ua.AttributeIds.AccessLevel))
                .Value.Value & ua.AccessLevel.CurrentWrite.mask)
        return command_writable, status_writable

    command_writable, status_writable = run(with_server(port, body))

    assert command_writable, "the supervisor cannot write a Command tag"
    assert not status_writable, "the supervisor can overwrite a Status tag"


def test_publishing_leaves_command_tags_alone(tmp_path: Path) -> None:
    """The machine must not write what it was asked for.

    Republishing Command every scan would erase a supervisor's request between
    the request and the machine acting on it.
    """
    port = free_port()

    async def body(nodes: dict[str, Any], tags: PackTags) -> int:
        node, variant = nodes["Command.MachSpeed"]
        await node.write_value(55, variant)
        tags.update(State.EXECUTE, StopReason.NONE, produced=1, rejected=0,
                    blocked=False, starved=False, speed=100)
        await publish(nodes, tags)
        return int(await node.read_value())

    assert run(with_server(port, body)) == 55


# --- modes --------------------------------------------------------------------
def test_a_mode_change_is_refused_while_the_machine_is_acting() -> None:
    """A machine in the middle of Starting is doing something, and changing the
    rules underneath it means the acting state completes into a state its new
    mode may not have."""
    from vpc.packtags import ModeChangeRefused

    tags = PackTags()
    with pytest.raises(ModeChangeRefused, match="acting state"):
        tags.request_mode(UnitMode.MANUAL, State.STARTING)


def test_a_mode_without_the_current_state_is_refused() -> None:
    from vpc.packtags import ModeChangeRefused

    tags = PackTags()
    with pytest.raises(ModeChangeRefused, match="no Complete state"):
        tags.request_mode(UnitMode.MANUAL, State.COMPLETE)


def test_a_mode_change_from_a_wait_state_is_accepted() -> None:
    tags = PackTags()

    assert tags.request_mode(UnitMode.MAINTENANCE, State.STOPPED) is \
        UnitMode.MAINTENANCE
    assert tags.status.unit_mode_current is UnitMode.MAINTENANCE
    assert not tags.command.unit_mode_change_request


def test_the_name_check_actually_fails_when_the_two_disagree(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A startup check nobody has watched fail is an assumption.

    The whole value of `check_names_agree` is catching a rename in one place and
    not the other. If it could not detect that, it would be a line of comfort
    that runs on every start and reports nothing.
    """
    from vpc import opcua

    monkeypatch.setattr(opcua, "browse_names",
                        lambda: {TagGroup.STATUS: ("NotATagAnybodyPublishes",)})

    with pytest.raises(ValueError, match="disagrees with PackTags"):
        opcua.check_names_agree()


def test_no_password_is_written_down_in_this_repository() -> None:
    """The credential must not exist in source, and this is what enforces it.

    A default password is a credential every clone and every future reader has,
    and "it is only a demo" is exactly how one ends up somewhere it is not. This
    test is the difference between having removed it once and it staying gone.
    """
    from vpc.opcua import Credentials

    account = Credentials()
    root = Path(__file__).resolve().parents[1]
    searched = 0
    for path in list(root.glob("src/**/*.py")) + list(root.glob("scripts/*.py")) \
            + list(root.glob("*.md")) + list(root.glob("docs/*.md")):
        searched += 1
        assert account.password not in path.read_text(encoding="utf-8"), (
            f"the password appears in {path.relative_to(root)}"
        )
    assert searched > 5, "the search found almost no files, so it proves nothing"


def test_a_generated_password_is_not_guessable() -> None:
    """Generated rather than defaulted, so the insecure option does not exist."""
    from vpc.opcua import Credentials

    password = Credentials().password

    assert len(password) >= 16, "a short generated password is barely better than none"
    assert password not in ("supervisor", "password", "vpc", "")
