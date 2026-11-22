# Проверка VPN на Linux-контроллере

`vpn_probe.py` поддерживает Linux amd64/arm64 и прежний Mac Apple Silicon.
На контроллере установщик запускается с `--test-route system --interface lo`.
`lo` здесь только неиспользуемая метка: Xray подключается к публичному IP
через системный маршрут, без привязки к loopback. Проверка использует один
аутентифицированный VLESS outbound; DIRECT fallback отсутствует.

Успешный результат подтверждает девять запросов через VPN из сети контроллера,
включая проверку выходного IP, передачу 1 МиБ и параллельные запросы.
Он сохраняет `testRoute=system`, `directReachabilityVerified=false` и
`physicalSourceObserved=false`. Это не подтверждение доступности из всех
российских операторов. Режим `physical` по-прежнему требует Mac и наблюдение
соединения на его физическом интерфейсе.

Движок — официальный [Xray v26.9.9](https://github.com/XTLS/Xray-core/releases/tag/v26.9.9).
SHA256 архивов сверены с digest официального GitHub release API; SHA256
извлечённых бинарников закреплены отдельно. При загрузке проверяются оба
хеша. Несовпадение или небезопасные права файла запрещают запуск.

| Linux | Архив | SHA256 бинарника |
| --- | --- | --- |
| amd64 | `Xray-linux-64.zip` | `c4ae6798c38e0e5343b192406746333cd0ba7ff3eb984f8c4b9939dcb68c3f8a` |
| arm64 | `Xray-linux-arm64-v8a.zip` | `c1defe42b6db958a97c5e049a02a00a4baaedaca7b51c1c229f0830e288acef5` |

Кэш — `$HOME/.spn-reality-deploy/runtime/xray-26.9.9-linux-amd64` либо
`xray-26.9.9-linux-arm64`. Существующие имена кэша и SHA256 Mac сохранены,
чтобы не менять его прежние отпечатки проверок. Неподходящая архитектура,
чужой бинарник или повреждённый кэш останавливают проверку.

Предварительно загрузить и проверить движок от имени службы на контроллере:

```sh
sudo -u spn-reserve -H env HOME=/var/lib/spn-reserve-fleet \
  /opt/spn-reserve-fleet/venv/bin/python - <<'PY'
import importlib.util
import platform
import subprocess

assert platform.system() == 'Linux'
path = '/opt/spn-reserve-fleet/current/ops/universal-reality/vpn_probe.py'
spec = importlib.util.spec_from_file_location('controller_probe', path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
engine = probe.select_engine()
subprocess.run([str(engine), 'version'], check=True, env=probe.e.ENV)
print('Verified engine SHA256:', probe.engine_spec()['sha256'])
PY
```

Для самой проверки по-прежнему нужен приватный файл тестовой подписки 0600,
принадлежащий пользователю службы. Пароли, ссылка подписки и конфигурация
пользователя не передаются в аргументах процесса и не включаются в результат.
