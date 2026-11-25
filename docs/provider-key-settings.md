# Provider keys in Settings

Administrators can save OpenAI and fal.ai keys under **Settings → Provider API
keys**. Saved keys are staged for later approved use. Saving, replacing, removing
or refreshing their status never contacts a provider or submits a test request.

## Administrator workflow

1. Sign in as a Studio administrator and open Settings.
2. Paste the complete key into the masked field for its provider and enter your
   current Studio account password. Use **Save** or **Replace** for that provider.
3. Review the stored-status confirmation. Key and password fields clear after
   submission; saved key values cannot be retrieved or revealed through the portal.

A blank key field preserves the current saved key. To remove it, leave the
replacement field blank, enter your administrator password and select **Remove**.
This removes only Studio's saved copy. It does not revoke the key at OpenAI or
fal.ai, and does not remove an independently configured runtime environment key.

Editors and viewers have no key-management controls or endpoint access. Each
change requires administrator reauthentication, an active session and a current
record revision. Rate limits fail closed. If another administrator changes the
record, the portal refreshes its status and requires a new deliberate submission;
it never automatically repeats a change. Responses and audit events contain
metadata, not key values, passwords, ciphertext or key fragments.

## Spending remains disabled

The portal provides no enable-generation or test-key button. Its saved status is
separate from runtime configuration: a securely stored key may coexist with a
runtime key marked **Not loaded**. Existing provider factories continue to use
separately approved environment credentials, not the staged database copies.

OpenAI director/storyboard calls and fal.ai video jobs have independent spending
gates, `VIDEO_DIRECTOR_ENABLED` and `VIDEO_PAID_GENERATION_ENABLED`. Both remain
false until the user approves the relevant test. Storing a key does not change
these gates, their approval references, model selection, budgets or worker state.
Narration, rendering and S3 also remain disabled in the initial VPS configuration.
Any later promotion of a staged key requires a reviewed operator change and the
appropriate explicit approval; saving it is not that approval.

## Operator storage setup

Migration **0006** adds the `provider_secrets` table, containing authenticated
Fernet ciphertext bound to the provider identity. Only revision, modification
time and actor metadata accompany it. Plaintext provider keys are never stored
in this table.

Configure an independent encryption master as
`VIDEO_PROVIDER_KEY_ENCRYPTION_KEY` in the existing root-owned, mode `0600` file
`/opt/video-studio/shared/studio.env`. Generate a fresh canonical
URL-safe Fernet key once using a secure operator process that writes directly
to protected storage without printing its value. Preserve it across application
updates; do not derive it from database, Redis or session credentials. Its value
belongs outside Git, images, frontend configuration and operational logs.

Compose passes this master only to the API service. Key management requires no
provider credentials in the frontend, no writable mount of the host environment
file, and no protected workloads secret or directory. An absent master leaves the application
running with secure storage unavailable; the portal disables key changes and the
API refuses them. Malformed nonempty master values fail configuration validation.
Treat the master as persistent secret material: losing or replacing it can make
existing saved keys unrecoverable. Rotation needs a separately reviewed procedure.

## Backup and recovery

Database backups may contain encrypted provider-key values. Keep custody of the
master separate from those backups. The isolated restore procedure deliberately
clears all restored key ciphertext, increments revisions even for empty
tombstones, and resets modification metadata. Restoring a backup therefore does
not restore usable staged provider credentials or revive stale edit revisions.
Re-enter any required keys through Settings after recovery and obtain fresh
approval before provider use. No backup or recovery operation grants spending
authorization.
