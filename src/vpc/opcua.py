"""An OPC UA server exposing the cell's PackTags, with security on.

WHY THIS IS IMPORTED AND MODBUS WAS NOT, since the two decisions look
inconsistent and are not. Modbus TCP is a header, a function code and a payload,
about two hundred lines, and writing it bought exhaustive testability of the one
layer that reads bytes off a socket. OPC UA is a binary protocol with a session
layer, a subscription model, a type system, an address space and X.509 security.
Implementing it would not be principled, it would be a year.

WHAT IT EXPOSES. The PackTags structure and nothing invented. Names come from
`vpc.packtags.browse_names()` rather than being written again here, because an
address space and a tag structure that disagree about a name is the same defect
as two copies of an address map, and it produces a supervisor that connects
successfully and reads nothing.

SECURITY IS ON, and that is the part worth having. The default in almost every
OPC UA demonstration is `SecurityPolicy None` with anonymous login, which is a
plaintext unauthenticated channel wearing the badge of a standard that has real
security in it. This server signs and encrypts with Basic256Sha256 and refuses
anonymous sessions. It is a self signed certificate, so it proves possession of
a key and nothing about identity, and that limit is written into the docs rather
than left for somebody to assume otherwise.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpc.isa95 import (
    WORK_UNIT_VARIABLES,
    EquipmentModel,
    WorkUnitSnapshot,
    default_model,
    snapshot,
    work_unit_values,
)
from vpc.packtags import PackTags, TagGroup, browse_names

#: A snapshot of nothing, used only to ask the information model which names it
#: publishes. Built once because `check_isa95_names_agree()` is called at
#: startup and the answer cannot change at runtime.
_EMPTY_SNAPSHOT: WorkUnitSnapshot = snapshot(
    PackTags(), producing_scans=0, planned_scans=0,
    good_count=0, reject_count=0)

#: Where the generated certificate and key live. Regenerated rather than
#: committed: a private key in a public repository is a private key no longer.
CERT_DIR = Path(__file__).resolve().parents[2] / "certs"
CERT = CERT_DIR / "cell-server.der"
KEY = CERT_DIR / "cell-server-key.pem"

NAMESPACE = "http://mkamel7.github.io/virtual-production-cell"
#: The server's identity, and it must appear in the certificate's subject
#: alternative names or clients reject the connection. The failure message they
#: give points at trust rather than at the mismatch, which sends people looking
#: in entirely the wrong place, so this is defined once and used in both.
APPLICATION_URI = "urn:mkamel7:virtual-production-cell"
#: 4841 and not the standard 4840, deliberately. A CODESYS Control Win runtime
#: exposes its OWN OPC UA server on 4840, and on this project that runtime is by
#: definition on the same machine. Binding 0.0.0.0:4840 alongside its
#: 127.0.0.1:4840 succeeds on Windows and then loopback clients silently reach
#: the runtime instead, which presents as BadSecurityPolicyRejected from a
#: server whose security is configured correctly. Found exactly that way.
DEFAULT_ENDPOINT = "opc.tcp://0.0.0.0:4841/vpc/"


#: The password for this run. Taken from the environment, or generated fresh if
#: there is none.
#:
#: There is NO default password in this repository and there is no file to put
#: one in. A credential committed to source control is a credential every clone
#: and every future reader has, and "it is only a demo" is exactly how one ends
#: up somewhere it is not. Generating instead of defaulting means the insecure
#: option does not exist rather than being discouraged.
#:
#: Evaluated once per process so a server and a client in the SAME process agree
#: without either being told. Across processes there is nothing to agree on, so
#: `scripts/serve_opcua.py` prints the generated password at startup and
#: VPC_OPCUA_PASSWORD is how you set a known one.
_PASSWORD: str = os.environ.get("VPC_OPCUA_PASSWORD") or secrets.token_urlsafe(18)

#: True when the password above was generated rather than supplied, so the
#: server can say so instead of leaving somebody guessing.
PASSWORD_WAS_GENERATED: bool = "VPC_OPCUA_PASSWORD" not in os.environ


@dataclass(frozen=True)
class Credentials:
    """The one account this server accepts.

    Anonymous is refused. A supervisory interface that anyone on the network can
    read and command is not a supervisory interface, it is an actuator with a
    nice browse tree.

    Set `VPC_OPCUA_USER` and `VPC_OPCUA_PASSWORD` to choose them. Neither has a
    value written down anywhere in this repository.
    """

    username: str = field(
        default_factory=lambda: os.environ.get("VPC_OPCUA_USER", "supervisor"))
    password: str = field(default_factory=lambda: _PASSWORD)


def ensure_certificate(cert: Path | None = None, key: Path | None = None,
                       common_name: str = "virtual-production-cell",
                       uri: str | None = None,
                       days: int = 365) -> tuple[Path, Path]:
    """Generate a self signed certificate and key if they are not there.

    Generated rather than committed. Checking a private key into a repository
    means every clone shares an identity, which is worse than having none
    because it looks like having one.

    Self signed is the honest limit: this proves the server holds the key that
    matches the certificate a client chose to trust. It proves nothing about who
    the server is. A real deployment issues from a CA the clients already trust,
    and the difference is the whole point of a PKI.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert = cert or CERT
    key_path = key or KEY
    uri = uri or APPLICATION_URI
    if cert.exists() and key_path.exists():
        return cert, key_path

    cert.parent.mkdir(parents=True, exist_ok=True)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)

    # The application URI must match the one the server advertises or clients
    # reject the certificate, and the failure message points at trust rather
    # than at the mismatch, which sends people looking in the wrong place.
    alt_names: list[x509.GeneralName] = [
        x509.UniformResourceIdentifier(uri),
        x509.DNSName(socket.gethostname()),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]

    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_encipherment=True,
                          data_encipherment=True, content_commitment=True,
                          key_agreement=False, key_cert_sign=False,
                          crl_sign=False, encipher_only=False,
                          decipher_only=False),
            critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                                   x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False)
        .sign(private, hashes.SHA256())
    )

    cert.write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    key_path.write_bytes(private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    return cert, key_path


def tag_values(tags: PackTags) -> dict[TagGroup, dict[str, Any]]:
    """The current value of every exposed tag, by group.

    Pure, so the mapping between PackTags and the address space can be tested
    without a server, a socket or an event loop. The async code below then has
    nothing in it worth testing except that it publishes what this returns.
    """
    status, admin, command = tags.status, tags.admin, tags.command
    return {
        TagGroup.COMMAND: {
            "UnitModeChangeRequest": command.unit_mode_change_request,
            "UnitModeRequested": int(command.unit_mode_requested),
            "MachSpeed": command.mach_speed,
        },
        TagGroup.STATUS: {
            "StateCurrent": status.state_code,
            "UnitModeCurrent": int(status.unit_mode_current),
            "StopReason": int(status.stop_reason),
            "SysReady": status.sys_ready,
            "EquipmentBlocked": status.equipment_blocked,
            "EquipmentStarved": status.equipment_starved,
            "MachSpeed": status.mach_speed,
            "CurMachSpeed": status.cur_mach_speed,
        },
        TagGroup.ADMIN: {
            "ProdProcessedCount": admin.prod_processed_count,
            "ProdDefectiveCount": admin.prod_defective_count,
            "AccTimeSinceReset": admin.acc_time_since_reset,
        },
    }


def check_names_agree() -> None:
    """The address space and the tag structure must name the same things.

    Called at startup rather than trusted. A supervisor that connects and finds
    a name missing reports nothing useful, and the fault looks like a network
    problem for the first hour of looking at it.
    """
    published = tag_values(PackTags())
    for group, names in browse_names().items():
        missing = set(names) - set(published[group])
        extra = set(published[group]) - set(names)
        if missing or extra:
            raise ValueError(
                f"{group.value} address space disagrees with PackTags: "
                f"missing {sorted(missing)}, unexpected {sorted(extra)}")


# --- the server itself -------------------------------------------------------
# Everything above is pure and tested without a socket. What follows is the thin
# async layer that publishes it, and the only thing worth verifying about it is
# that a real client can connect under security and read what `tag_values`
# returns. `tests/test_opcua.py` does exactly that.

async def build_address_space(server: Any, tags: PackTags
                              ) -> dict[str, tuple[Any, Any]]:
    """Create the Cell object and its three folders. Returns node by tag name.

    Names are prefixed by group, so `Status.StateCurrent` and `Command.MachSpeed`
    do not collide with `Status.MachSpeed`. PackTags genuinely has that name in
    two groups, because the supervisor asks for a speed and the machine reports
    one, and flattening them would silently drop a tag.
    """
    from asyncua import ua

    index = await server.register_namespace(NAMESPACE)
    cell = await server.nodes.objects.add_object(index, "Cell")
    values = tag_values(tags)
    nodes: dict[str, tuple[Any, Any]] = {}

    for group in (TagGroup.COMMAND, TagGroup.STATUS, TagGroup.ADMIN):
        folder = await cell.add_folder(index, group.value)
        for name, value in values[group].items():
            variant = ua.VariantType.Boolean if isinstance(value, bool) \
                else ua.VariantType.Int32
            node = await folder.add_variable(index, name, value, variant)
            # Command tags are the supervisor's to write. Status and Admin are
            # the machine's, and a supervisor able to write them could overwrite
            # the machine's own view of itself.
            if group is TagGroup.COMMAND:
                await node.set_writable()
            # The variant travels with the node. Without it a later write of a
            # plain Python int is inferred as Int64 and the server refuses it
            # as a type mismatch against an Int32 attribute, which reads as a
            # protocol fault and is a bookkeeping one.
            nodes[f"{group.value}.{name}"] = (node, variant)
    return nodes


async def publish(nodes: dict[str, tuple[Any, Any]], tags: PackTags) -> None:
    """Push the current tag values into the address space.

    Command tags are skipped: they belong to the supervisor, and a machine that
    wrote them every scan would erase whatever it had just been asked for.
    """
    values = tag_values(tags)
    for group, group_values in values.items():
        if group is TagGroup.COMMAND:
            continue
        for name, value in group_values.items():
            node, variant = nodes[f"{group.value}.{name}"]
            await node.write_value(value, variant)


def check_isa95_names_agree() -> None:
    """The ISA-95 variables published must be the ones declared.

    The same check as `check_names_agree()` and for the same reason, against the
    other half of the address space. `isa95.WORK_UNIT_VARIABLES` is what a
    supervisor browses for; `isa95.work_unit_values()` is what actually gets
    written. A name in one and not the other is a tag that reads as absent.
    """
    published = tuple(work_unit_values(_EMPTY_SNAPSHOT))
    if published != WORK_UNIT_VARIABLES:
        raise ValueError(
            "ISA-95 address space disagrees with the information model: "
            f"declared {WORK_UNIT_VARIABLES}, publishes {published}")


def _variant_for(value: object, ua: Any) -> Any:
    """Pick the OPC UA type for a published value.

    Float before int, because `isinstance(True, int)` is also true and a KPI
    ratio that arrived as an Int32 would be published as 0 or 1. There are no
    booleans in the ISA-95 variable set today, and this ordering means adding
    one later cannot silently truncate a ratio.
    """
    if isinstance(value, float):
        return ua.VariantType.Double
    if isinstance(value, int):
        return ua.VariantType.Int32
    return ua.VariantType.String


async def build_isa95_address_space(
        server: Any, snap: WorkUnitSnapshot,
        model: EquipmentModel | None = None) -> dict[str, tuple[Any, Any]]:
    """Create the equipment hierarchy as nested objects, variables on the unit.

    The hierarchy is built from `EquipmentModel.walk()` rather than written out
    here, so the address space cannot describe a different plant from the one
    the information model describes. That is the same discipline as taking tag
    names from `packtags.browse_names()`.

    Only the work unit carries variables. The levels above it are addresses, not
    machines: a Site has no MachineState, and giving it one would invite a
    supervisor to aggregate across a level that never populated it.
    """
    from asyncua import ua

    model = model or default_model()
    index = await server.register_namespace(NAMESPACE)
    objects: dict[str, Any] = {}
    nodes: dict[str, tuple[Any, Any]] = {}

    # The node itself is not needed here, only its position: `walk()` yields
    # parents before children, so the parent object always exists by the time a
    # child asks for it, and the path is the whole address.
    for path, _node in model.walk():
        parent_path, _, name = path.rpartition("/")
        parent = objects[parent_path] if parent_path else server.nodes.objects
        objects[path] = await parent.add_object(index, name)

    unit = objects[snap.path]
    for name, value in work_unit_values(snap).items():
        variant = _variant_for(value, ua)
        variable = await unit.add_variable(index, name, value, variant)
        # Every one of these is the machine's own view of itself. None of them
        # is writable: a supervisor able to set OEE could report a line as
        # healthy that is not, which is worse than having no figure at all.
        nodes[f"{snap.path}.{name}"] = (variable, variant)
    return nodes


async def publish_isa95(nodes: dict[str, tuple[Any, Any]],
                        snap: WorkUnitSnapshot) -> None:
    """Push the current information-model values into the address space."""
    for name, value in work_unit_values(snap).items():
        node, variant = nodes[f"{snap.path}.{name}"]
        await node.write_value(value, variant)


async def make_server(endpoint: str = DEFAULT_ENDPOINT,
                      credentials: Credentials | None = None) -> Any:
    """A server with signing, encryption and anonymous login refused.

    The default in almost every OPC UA demonstration is SecurityPolicy None with
    anonymous access, which is a plaintext unauthenticated channel wearing the
    badge of a standard that has real security in it.
    """
    # asyncua re-exports these without __all__, so mypy strict cannot see them.
    # Ignored at the import rather than sprinkled through the body, and narrowly,
    # so a genuine typo in a name here still fails.
    from asyncua import Server, ua  # type: ignore[attr-defined]
    from asyncua.server.user_managers import (  # type: ignore[attr-defined]
        User,
        UserManager,
        UserRole,
    )

    account = credentials or Credentials()

    class OnlyTheSupervisor(UserManager):
        def get_user(self, iserver: Any, username: str | None = None,
                     password: str | None = None,
                     certificate: Any = None) -> User | None:
            if username == account.username and password == account.password:
                return User(role=UserRole.User)
            return None            # anonymous and everyone else

    server = Server(user_manager=OnlyTheSupervisor())
    await server.init()
    server.set_endpoint(endpoint)
    server.set_server_name("Virtual Production Cell")
    # Awaited. It is a coroutine, and calling it without awaiting silently does
    # nothing: the server keeps the default urn:freeopcua:python:server, which
    # then does not match the certificate and every real client refuses it.
    await server.set_application_uri(APPLICATION_URI)

    certificate, key = ensure_certificate()
    await server.load_certificate(str(certificate))
    await server.load_private_key(str(key))
    server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
    # Anonymous is not in the list, so it is not offered. Username is.
    server.set_identity_tokens([ua.UserNameIdentityToken])
    return server
