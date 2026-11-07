# Register your sign-in app

proton2m365 does not ship a sign-in app: it signs in with one you register, so your mail is never accessed
through anyone else's app. This is a one-time setup. You need the app's **Application (client) ID** and a
**tenant** value at the end.

Which route?

- **Work or school account** (Microsoft 365 Business, Enterprise, Education): route A. Needs an administrator of
  your organization, about 10 minutes.
- **Personal account** (Outlook.com, Hotmail, Microsoft 365 Personal or Family): route B. Microsoft lets only
  organizations register apps, so you first create a free Azure account, which gives you a small organization of
  your own. About 20 minutes; Azure asks for a card to verify you, and registering an app costs nothing.
  Personal accounts are experimental in v0.1.

## Route A: work or school account

1. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com) as an administrator (at least
   Application Administrator, or a user allowed to register apps).
2. **Entra ID → App registrations → New registration**:
   - Name: `proton2m365` (people see it when they sign in).
   - Supported account types: **Accounts in this organizational directory only** (single tenant).
   - Redirect URI: platform **Public client/native (mobile & desktop)**, value `http://localhost`.
   - **Register**. Copy the **Application (client) ID** and the **Directory (tenant) ID** from the Overview page.
3. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**: add `Mail.ReadWrite`,
   `MailboxSettings.ReadWrite` and `IMAP.AccessAsUser.All`. Then select **Grant admin consent for <your
   organization>**.
4. **Authentication → Advanced settings → Allow public client flows: Yes**, and **Save** (needed for
   `--device-code`).
5. Make sure IMAP is allowed for the mailbox you migrate into: in Exchange PowerShell,
   `Set-CASMailbox <address> -ImapEnabled $true`. Security defaults and Conditional Access must allow the sign-in.

Tenant value: the **Directory (tenant) ID** (or your domain, e.g. `example.com`).

## Route B: personal account (Outlook.com)

1. In a private browser window, sign up for a [free Azure account](https://azure.microsoft.com/free) with any
   Microsoft account. This creates a directory (a tenant) where you are the administrator. You do not need to
   create any Azure resources.
2. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com) with that account.
3. **Entra ID → App registrations → New registration**:
   - Name: `proton2m365`.
   - Supported account types: **Personal Microsoft accounts only**.
   - Redirect URI: platform **Public client/native (mobile & desktop)**, value `http://localhost`.
   - **Register**. Copy the **Application (client) ID**.
4. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**: add `Mail.ReadWrite`,
   `MailboxSettings.ReadWrite` and `IMAP.AccessAsUser.All`. (You consent for yourself the first time you sign in.)
5. **Authentication → Advanced settings → Allow public client flows: Yes**, and **Save**.

Tenant value: `consumers`.

## Tell proton2m365

Put both values in `~/.config/proton2m365/config.toml` (`%USERPROFILE%\.config\proton2m365\config.toml` on Windows):

```toml
client_id = "<application id>"
tenant = "<tenant id or domain, or consumers>"
```

(on Windows, write paths in single quotes, `state = 'C:\Users\you\state.sqlite'`, because backslashes inside double
quotes are escape sequences in TOML), or pass `--client-id` and `--tenant` to each command, or set `PROTON2M365_CLIENT_ID` and `PROTON2M365_TENANT`.
Flags win over the environment, which wins over the file. Then check everything with:

```sh
proton2m365 selftest
```

The same file (and `PROTON2M365_*` variables) can also hold `state`, `account`, `max_msgs_per_sec` and
`max_bytes_per_sec`; `proton2m365 doctor` shows where each setting came from.

If sign-in fails, the message starts with a code explained in [errors.md](errors.md): `P2M-AUTH-TENANT` means the
tenant value is missing or wrong, `P2M-AUTH-CONSENT` means step A3's "Grant admin consent" was not done.
