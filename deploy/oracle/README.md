# Running the trader on Oracle Cloud (Always Free, Ampere A1)

The bot runs 24/7 on your Oracle server, so your PC can be off. You see the dashboard through an
encrypted SSH tunnel. Nothing on the server is open to the internet except SSH (key login only):
Oracle's network rules and the server's own firewall both allow port 22 only. Keep it that way,
because the dashboard has no login.

> **Never run the bot on the PC and the server at the same time.** Both would trade the same
> Webull account.

**Server:** Ubuntu 24.04 (ARM), user `ubuntu`, 1 OCPU / 6 GB. The public IP is shown under
Compute → Instances in the Oracle console. Below it is written `PUBLIC_IP`.

**SSH key:** `%USERPROFILE%\.ssh\oracle_trader.key` (a copy of the `.key` file Oracle gave you).
Keep the original somewhere safe: without it you cannot log in.

---

## Moving the bot to the server

Already done once: the code was copied, `deploy/oracle/server_setup.sh` installed Python and the
bot's packages, and the background service is registered (not started).

1. **Stop the bot on the PC:** run `Stop_Trader.bat`, and leave it stopped from now on.
2. **Copy the database and settings** from the trader folder on the PC:

   ```powershell
   cd C:\Users\User\Downloads\Trader
   powershell -ExecutionPolicy Bypass -File deploy\oracle\push_to_server.ps1 -Server ubuntu@PUBLIC_IP
   ```

   It refuses to run while the bot is still running on the PC, or if the server already has a
   database.
3. **Log in to the server and to Webull, then start the bot:**

   ```powershell
   ssh -i $env:USERPROFILE\.ssh\oracle_trader.key ubuntu@PUBLIC_IP
   ```

   ```bash
   cd ~/trader
   ./auth.sh                            # approve the request in the Webull app
   systemctl --user start ai-trader
   systemctl --user status ai-trader    # should say "active (running)"
   tail -f logs/app.log                 # Ctrl+C to stop watching
   ```

   You can now close the window and turn the PC off.

## Opening the dashboard

**From the PC:** open a tunnel and leave the window open while you use the dashboard:

```powershell
ssh -i $env:USERPROFILE\.ssh\oracle_trader.key -N -L 8000:127.0.0.1:8000 ubuntu@PUBLIC_IP
```

Then browse to <http://127.0.0.1:8000>.

**From the iPhone:** install **Termius** (the free tier is enough).
1. Keychain → **Import** the `oracle_trader.key` file (AirDrop or iCloud it to the phone).
2. Hosts → add `PUBLIC_IP`, username `ubuntu`, key `oracle_trader.key`.
3. **Port Forwarding → Local:** local port `8000`, destination `127.0.0.1`, destination port `8000`.
4. Start the forward and open <http://127.0.0.1:8000> in Safari.

iOS pauses the tunnel when Termius has been in the background for a while. Reopen Termius to
reconnect.

---

## Everyday tasks

On the server (`ssh -i $env:USERPROFILE\.ssh\oracle_trader.key ubuntu@PUBLIC_IP`, then `cd ~/trader`):

| Task | Command |
|---|---|
| Is it running? | `systemctl --user status ai-trader` |
| Live log | `tail -f logs/app.log` |
| Stop / start / restart | `systemctl --user stop ai-trader` (or `start`, `restart`) |
| Renew the Webull login (about every 15 days; the bot alerts you first) | `./auth.sh`, then `systemctl --user restart ai-trader` |
| Back up the database | `sqlite3 data/trader.db ".backup data/backup.db"`, then on the PC: `scp -i $env:USERPROFILE\.ssh\oracle_trader.key ubuntu@PUBLIC_IP:trader/data/backup.db .` |
| Memory and disk | `free -h` and `df -h /` |

**Sending code changes from the PC.** Build the dashboard first if you changed it
(`cd web; npm run build`), then:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\oracle\push_to_server.ps1 -Server ubuntu@PUBLIC_IP -CodeOnly
```

`-CodeOnly` never touches the server's database, `.env` or Webull login. It restarts the bot if
the bot was running.

**Changing settings.** Edit `~/trader/.env` on the server (`nano .env`), then restart the bot.
The `.env` on the PC is no longer the live copy.

**Keep on the PC:** dashboard builds (`npm run build`). Backtests also run fine on the PC.

---

## Staying inside the free limits

- Keep all Ampere A1 instances together at **2 OCPUs and 12 GB or less** (this one is 1 / 6).
- Storage: **200 GB total** (boot and block volumes) and **5 volume backups** at most. Leave the
  boot volume without a backup policy.
- Create nothing in other regions, and no other instance shapes (only A1 within the limit and
  up to two VM.Standard.E2.1.Micro are free).
- Budget alert: Billing & Cost Management → Budgets, $1 a month, alert at 1% actual spend.
- Check Billing & Cost Management → Cost Analysis now and then: it should say $0.00.

## Moving back to the PC

1. Server: `systemctl --user stop ai-trader && systemctl --user disable ai-trader`
2. PC: `scp -i $env:USERPROFILE\.ssh\oracle_trader.key -r ubuntu@PUBLIC_IP:trader/data .\data_from_server`,
   then replace `data\` with it (keep a copy of the old one).
3. Start the bot on the PC as before.
