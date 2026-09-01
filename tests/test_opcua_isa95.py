"""The ISA-95 half of the address space, browsed by a real client.

Same standard of proof as `test_opcua.py`: the server agreeing with itself
proves nothing, so this connects with `asyncua`'s client over an encrypted
channel and walks the hierarchy the way a supervisor or UaExpert would.

The claim being tested is the one the flat tag space could not make: that a
work unit is addressed by WHERE IT SITS. So the test browses
Enterprise -> Site -> Packaging -> BottlingLine1 -> PackagingCell by name at
each level, rather than reading a node reference it was handed, because being
handed the node is exactly the shortcut that would let a broken hierarchy pass.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from vpc.isa95 import (
    CELL_PATH,
    WORK_UNIT_VARIABLES,
    Recipe,
    default_model,
    snapshot,
)
from vpc.opcua import (
    Credentials,
    build_isa95_address_space,
    check_isa95_names_agree,
    ensure_certificate,
    make_server,
    publish_isa95,
)
from vpc.packml import State
from vpc.packtags import PackTags, StopReason

T = TypeVar("T")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run(coroutine: Awaitable[T]) -> T:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def _snap(state: State = State.EXECUTE,
          reason: StopReason = StopReason.NONE,
          *, good: int = 90, reject: int = 10):
    tags = PackTags()
    tags.status.state_current = state
    tags.status.stop_reason = reason
    return snapshot(tags, producing_scans=300, planned_scans=400,
                    good_count=good, reject_count=reject)


# --- the pure half -----------------------------------------------------------
def test_the_isa95_address_space_and_the_model_name_the_same_things() -> None:
    """Two copies of a name list is one copy plus a future defect."""
    check_isa95_names_agree()


def test_the_isa95_name_check_actually_fails_when_the_two_disagree(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A startup check nobody has watched fail is an assumption.

    The same argument as `test_the_name_check_actually_fails_when_the_two
    _disagree` in `test_opcua.py`: the whole value of this check is catching a
    rename made in one place and not the other, so it has to be seen catching
    one at least once.
    """
    from vpc import opcua

    monkeypatch.setattr(opcua, "WORK_UNIT_VARIABLES", ("NotAVariableWePublish",))

    with pytest.raises(ValueError, match="disagrees with the information model"):
        opcua.check_isa95_names_agree()


def test_a_ratio_is_published_as_a_double_not_truncated_to_an_integer() -> None:
    """`isinstance(True, int)` is why float is checked first.

    A KPI ratio published as Int32 arrives at the supervisor as 0 or 1, which
    looks like a line that is either perfect or dead.
    """
    from asyncua import ua

    from vpc.opcua import _variant_for

    assert _variant_for(0.75, ua) is ua.VariantType.Double
    assert _variant_for(90, ua) is ua.VariantType.Int32
    assert _variant_for("Execute", ua) is ua.VariantType.String


# --- a real client -----------------------------------------------------------
async def with_server(port: int, snap: Any,
                      body: Callable[[Any], Awaitable[T]]) -> T:
    server = await make_server(f"opc.tcp://127.0.0.1:{port}/vpc/")
    nodes = await build_isa95_address_space(server, snap)
    async with server:
        await publish_isa95(nodes, snap)
        return await body(nodes)


async def connect(port: int, tmp_path: Path) -> Any:
    from asyncua import Client

    account = Credentials()
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


def test_a_supervisor_browses_the_hierarchy_level_by_level(
        tmp_path: Path) -> None:
    """The claim a flat tag list cannot make: equipment addressed by position."""
    port = free_port()
    snap = _snap()

    async def body(_: Any) -> dict[str, float]:
        client = await connect(port, tmp_path)
        async with client:
            index = await client.get_namespace_index(
                "http://mkamel7.github.io/virtual-production-cell")
            node = client.nodes.objects
            for name in CELL_PATH.split("/"):
                node = await node.get_child(f"{index}:{name}")
            oee = await node.get_child(f"{index}:OEE")
            quality = await node.get_child(f"{index}:Quality")
            return {"oee": await oee.read_value(),
                    "quality": await quality.read_value()}

    values = run(with_server(port, snap, body))
    assert values["quality"] == pytest.approx(0.9)
    assert values["oee"] == pytest.approx(snap.kpi.oee)


def test_every_declared_variable_exists_on_the_work_unit(
        tmp_path: Path) -> None:
    """A declared name that is not in the address space reads as absent.

    `get_variables()` rather than `get_children()`: the work unit's children are
    the five equipment modules AS WELL AS these variables, which is the
    hierarchy doing its job. The first version of this test compared against
    children and failed for that reason.
    """
    port = free_port()

    async def body(_: Any) -> set[str]:
        client = await connect(port, tmp_path)
        async with client:
            index = await client.get_namespace_index(
                "http://mkamel7.github.io/virtual-production-cell")
            node = client.nodes.objects
            for name in CELL_PATH.split("/"):
                node = await node.get_child(f"{index}:{name}")
            names = set()
            for child in await node.get_variables():
                names.add((await child.read_browse_name()).Name)
            return names

    assert run(with_server(port, _snap(), body)) == set(WORK_UNIT_VARIABLES)


def test_the_work_unit_still_carries_its_equipment_modules(
        tmp_path: Path) -> None:
    """The other half of the check above, so neither can quietly disappear."""
    port = free_port()

    async def body(_: Any) -> set[str]:
        client = await connect(port, tmp_path)
        async with client:
            index = await client.get_namespace_index(
                "http://mkamel7.github.io/virtual-production-cell")
            node = client.nodes.objects
            for name in CELL_PATH.split("/"):
                node = await node.get_child(f"{index}:{name}")
            names = set()
            for child in await node.get_children():
                if child not in await node.get_variables():
                    names.add((await child.read_browse_name()).Name)
            return names

    assert run(with_server(port, _snap(), body)) == {
        "Infeed", "Filler", "Capper", "QCStation", "Outfeed"}


def test_the_levels_above_the_work_unit_carry_no_machine_variables(
        tmp_path: Path) -> None:
    """A Site has no MachineState, and inventing one invites bad aggregation."""
    port = free_port()

    async def body(_: Any) -> int:
        client = await connect(port, tmp_path)
        async with client:
            index = await client.get_namespace_index(
                "http://mkamel7.github.io/virtual-production-cell")
            site = await (await client.nodes.objects.get_child(
                f"{index}:Enterprise")).get_child(f"{index}:Site")
            return len(await site.get_variables())

    assert run(with_server(port, _snap(), body)) == 0


def test_a_supervisor_cannot_write_the_machines_own_kpis(
        tmp_path: Path) -> None:
    """Writable OEE means a line can be reported healthy when it is not."""
    from asyncua import ua

    port = free_port()

    async def body(_: Any) -> None:
        client = await connect(port, tmp_path)
        async with client:
            index = await client.get_namespace_index(
                "http://mkamel7.github.io/virtual-production-cell")
            node = client.nodes.objects
            for name in CELL_PATH.split("/"):
                node = await node.get_child(f"{index}:{name}")
            oee = await node.get_child(f"{index}:OEE")
            with pytest.raises(ua.UaStatusCodeError):
                await oee.write_value(1.0, ua.VariantType.Double)

    run(with_server(port, _snap(), body))


def test_republishing_moves_the_published_values(tmp_path: Path) -> None:
    """The address space has to follow the machine, not snapshot it once."""
    port = free_port()
    first = _snap(good=90, reject=10)
    later = _snap(good=180, reject=20)

    async def body(nodes: Any) -> int:
        await publish_isa95(nodes, later)
        client = await connect(port, tmp_path)
        async with client:
            index = await client.get_namespace_index(
                "http://mkamel7.github.io/virtual-production-cell")
            node = client.nodes.objects
            for name in CELL_PATH.split("/"):
                node = await node.get_child(f"{index}:{name}")
            good = await node.get_child(f"{index}:GoodCount")
            return int(await good.read_value())

    assert run(with_server(port, first, body)) == 180


def test_a_custom_model_can_be_published_instead_of_the_default() -> None:
    """The builder must follow the model it is given, not a hard-coded tree."""
    port = free_port()
    model = default_model()
    snap = snapshot(PackTags(), producing_scans=1, planned_scans=1,
                    good_count=1, reject_count=0,
                    recipe=Recipe(name="Custom"))

    async def body(nodes: Any) -> int:
        return len(nodes)

    async def go() -> int:
        server = await make_server(f"opc.tcp://127.0.0.1:{port}/vpc/")
        nodes = await build_isa95_address_space(server, snap, model)
        async with server:
            await publish_isa95(nodes, snap)
            return await body(nodes)

    assert run(go()) == len(WORK_UNIT_VARIABLES)
