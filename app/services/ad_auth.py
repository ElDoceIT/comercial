import ssl
from dataclasses import dataclass

from ldap3 import ALL, SIMPLE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app.core.config import get_settings


class ActiveDirectoryAuthError(Exception):
    pass


@dataclass(frozen=True)
class ActiveDirectoryUser:
    username: str
    display_name: str
    email: str | None
    distinguished_name: str


@dataclass(frozen=True)
class ActiveDirectoryGroupUser:
    username: str
    display_name: str
    first_name: str
    last_name: str
    email: str | None


def _required_setting(name: str) -> str:
    value = getattr(get_settings(), name.lower())
    if not value:
        raise ActiveDirectoryAuthError(f"Falta configurar {name}.")
    return value


def _server() -> Server:
    settings = get_settings()
    validate = ssl.CERT_REQUIRED if settings.ad_tls_validate_cert else ssl.CERT_NONE
    tls = Tls(validate=validate)
    return Server(
        _required_setting("AD_SERVER_IP"),
        port=settings.ad_server_port,
        use_ssl=settings.ad_use_ssl,
        tls=tls,
        get_info=ALL,
        connect_timeout=settings.ad_connect_timeout,
    )


def _bind_user() -> str:
    settings = get_settings()
    bind_user = _required_setting("AD_BIND_USER")
    domain = settings.ad_domain
    if "\\" not in bind_user and "@" not in bind_user and domain:
        return f"{bind_user}@{domain}"
    return bind_user


def _configured_group_dn(connection: Connection) -> str:
    settings = get_settings()
    group_name = _required_setting("AD_GROUP_NAME")
    base_dn = _required_setting("AD_BASE_DN")
    groups_base_dn = settings.ad_groups_base_dn or base_dn
    escaped_group = escape_filter_chars(group_name)
    connection.search(
        groups_base_dn,
        f"(&(objectClass=group)(cn={escaped_group}))",
        attributes=["distinguishedName"],
        size_limit=1,
    )
    if not connection.entries:
        raise ActiveDirectoryAuthError("No se encontró el grupo habilitado en AD.")
    return connection.entries[0].entry_dn


def list_ad_group_users() -> list[ActiveDirectoryGroupUser]:
    login_attr = get_settings().ad_login_attr
    base_dn = _required_setting("AD_BASE_DN")

    try:
        with Connection(
            _server(),
            user=_bind_user(),
            password=_required_setting("AD_BIND_PASSWORD"),
            authentication=SIMPLE,
            auto_bind=True,
        ) as service_connection:
            group_dn = _configured_group_dn(service_connection)
            escaped_group_dn = escape_filter_chars(group_dn)
            service_connection.search(
                base_dn,
                (
                    "(&"
                    "(objectClass=user)"
                    f"(memberOf:1.2.840.113556.1.4.1941:={escaped_group_dn})"
                    ")"
                ),
                attributes=[login_attr, "displayName", "givenName", "sn", "mail"],
            )
            users: list[ActiveDirectoryGroupUser] = []
            for entry in service_connection.entries:
                username = str(entry[login_attr] or "").strip()
                if not username:
                    continue
                display_name = str(entry.displayName or "").strip()
                if not display_name:
                    display_name = " ".join(
                        part
                        for part in (
                            str(entry.givenName or "").strip(),
                            str(entry.sn or "").strip(),
                        )
                        if part
                    )
                users.append(ActiveDirectoryGroupUser(
                    username=username,
                    display_name=display_name or username,
                    first_name=str(entry.givenName or "").strip() or display_name or username,
                    last_name=str(entry.sn or "").strip() or "-",
                    email=str(entry.mail or "").strip() or None,
                ))
            return sorted(users, key=lambda user: (user.display_name.lower(), user.username.lower()))
    except ActiveDirectoryAuthError:
        raise
    except LDAPException as error:
        raise ActiveDirectoryAuthError(
            "No se pudo consultar el grupo de Active Directory."
        ) from error


def authenticate_ad_user(username: str, password: str) -> ActiveDirectoryUser:
    username = username.strip()
    if not username or not password:
        raise ActiveDirectoryAuthError("Ingresá usuario y contraseña.")

    login_attr = get_settings().ad_login_attr
    base_dn = _required_setting("AD_BASE_DN")

    try:
        with Connection(
            _server(),
            user=_bind_user(),
            password=_required_setting("AD_BIND_PASSWORD"),
            authentication=SIMPLE,
            auto_bind=True,
        ) as service_connection:
            group_dn = _configured_group_dn(service_connection)

            escaped_username = escape_filter_chars(username)
            escaped_group_dn = escape_filter_chars(group_dn)
            service_connection.search(
                base_dn,
                (
                    "(&"
                    "(objectClass=user)"
                    f"({login_attr}={escaped_username})"
                    f"(memberOf:1.2.840.113556.1.4.1941:={escaped_group_dn})"
                    ")"
                ),
                attributes=[login_attr, "displayName", "givenName", "sn", "mail"],
                size_limit=1,
            )
            if not service_connection.entries:
                raise ActiveDirectoryAuthError("El usuario no está habilitado para esta aplicación.")

            entry = service_connection.entries[0]
            user_dn = entry.entry_dn

        with Connection(
            _server(),
            user=user_dn,
            password=password,
            authentication=SIMPLE,
            auto_bind=True,
        ):
            display_name = str(entry.displayName or "").strip()
            if not display_name:
                display_name = " ".join(
                    part for part in [str(entry.givenName or "").strip(), str(entry.sn or "").strip()]
                    if part
                )
            return ActiveDirectoryUser(
                username=username,
                display_name=display_name or username,
                email=str(entry.mail or "").strip() or None,
                distinguished_name=user_dn,
            )
    except ActiveDirectoryAuthError:
        raise
    except LDAPException as error:
        raise ActiveDirectoryAuthError("No se pudo validar el usuario contra Active Directory.") from error
