# RR Trading One — Backup and recovery manual

Applies to the native Windows installation (`C:\Program Files\RR Trading One`, data in
`C:\ProgramData\RR Trading One`). Run every command in **PowerShell as Administrator**.

## 1. What is protected and how
| Item | Location | Protection |
|---|---|---|
| Database (all trades, strategies, research, backtests, audit) | `ProgramData\RR Trading One\data\postgresql` | Daily encrypted backup; loopback-only access; password (SCRAM) |
| Backups | `ProgramData\RR Trading One\backups\*.rrbak` | AES-256-GCM, manifest-authenticated; folder readable by SYSTEM and Administrators only |
| Backup key | `ProgramData\RR Trading One\secrets\backup.key` | Separate from the engine secrets. **Without it no backup can be read.** |
| Engine secrets (DB/Redis passwords, encryption key) | `ProgramData\RR Trading One\secrets\engine.env` | SYSTEM and Administrators; each engine service account can read this one file |

**Do this once, right after installation:** copy `backup.key` **and** `engine.env` to an offline, safe place
(an encrypted USB stick or a password manager). Both are needed to recover on a new machine:
* `backup.key` decrypts the backups;
* `RR_ENCRYPTION_KEY` in `engine.env` decrypts credentials stored inside the database.

## 2. Automatic backups
A scheduled task, **RR Trading One - Backup**, runs daily at 02:17 as SYSTEM and keeps the newest 14 backups. Its
output goes to `ProgramData\RR Trading One\logs\backup.log`.

`provision.ps1` (install or upgrade) also takes a verified backup **before** applying any database migration to an
existing installation.

## 3. Manual backup, verification and listing
```powershell
cd "C:\Program Files\RR Trading One\infrastructure"
```
```powershell
.\backup.ps1 -Create -Label before-change
```
```powershell
.\backup.ps1 -Verify -File "C:\ProgramData\RR Trading One\backups\<file>.rrbak"
```
Verify decrypts the whole file in memory-safe chunks. Any changed byte, missing chunk, wrong key or edited manifest
makes it fail.

## 4. Restore (never overwrites the running database)
Restore always creates a **new** database and checks every table's row count against the backup's manifest:
```powershell
.\backup.ps1 -Restore -File "C:\ProgramData\RR Trading One\backups\<file>.rrbak" -TargetDb rr_restore_20261113
```
The result is printed as JSON; `"ok": true` means every table matched.

### Switching the application to the restored database (explicit, reversible)
1. Stop the engine services (the trading worker first):
   ```powershell
   Stop-Service RRTradingOne-Worker, RRTradingOne-Learner, RRTradingOne-API
   ```
2. In `C:\ProgramData\RR Trading One\secrets\engine.env`, change the database name at the end of
   `RR_DATABASE_URL` from `/rr_trading` to `/rr_restore_20261113`. Keep the old line as a comment so you can switch
   back.
3. Start the API and learner. The worker stays stopped until you start it deliberately:
   ```powershell
   Start-Service RRTradingOne-API, RRTradingOne-Learner
   ```
4. Check:
   ```powershell
   .\status.ps1
   ```
   Then sign in and confirm your data.

The previous database is untouched; to roll back, restore the old `RR_DATABASE_URL` line and restart the services.

## 5. Recovering on a new machine
1. Install RR Trading One on the new machine.
2. Stop the engine services (step 4.1 above).
3. Replace `engine.env` with your saved copy, and copy `backup.key` into the `secrets` folder.
4. Copy the backup file into `ProgramData\RR Trading One\backups`.
5. Restore into a new database (section 4), switch to it (section 4, steps 2–4), then sign in and check.

## 6. Uninstalling
`uninstall.ps1` removes services and program files and **keeps** `C:\ProgramData\RR Trading One` (database,
backups, secrets, logs). Deleting user data requires both flags:
```powershell
.\uninstall.ps1 -RemoveUserData -ConfirmRemoveUserData "DELETE MY DATA"
```

## 7. What not to do
* Don't copy the PostgreSQL data folder between machines or PostgreSQL versions; use backup and restore.
* Don't delete `backup.key` or `engine.env` without an offline copy.
* Don't restore into the database in use: the tool refuses this on purpose.
