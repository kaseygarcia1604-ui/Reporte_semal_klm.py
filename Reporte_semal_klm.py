"""
REPORTE SEMANAL -- servicio independiente, NO toca V7.1PY

Se conecta a la misma Postgres (DATABASE_URL) solo en modo LECTURA,
calcula metricas de la ultima semana, las compara contra la linea base
del backtest validado, y manda el resumen por Telegram. Pensado para
correr como cron semanal en Railway (mismo patron que AVAL_agente),
NO como servicio persistente -- se ejecuta una vez, manda el reporte,
termina.

Variables de entorno necesarias (las mismas que ya existen en el
proyecto, se pueden compartir sin duplicar):
  DATABASE_URL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

Baseline del backtest validado (4 meses, ~9 meses de calendario):
  55 trades, Win Rate 70.91%, Ratio 4.61, p-valor 0.0013
Estos numeros son la vara de comparacion -- el reporte no juzga si
"esta bien o mal", solo muestra el contraste para que la decision la
tome la persona, no el script.
"""
import os
import sys
import logging
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
log = logging.getLogger("reporte_semanal")

BASELINE = {
    "win_rate": 70.91,
    "ratio": 4.61,
    "trades": 55,
    "periodo_meses_calendario": 9,
}


def enviar_telegram(token: str, chat_id: str, texto: str):
    if not token or not chat_id:
        log.warning("Sin TELEGRAM_TOKEN/TELEGRAM_CHAT_ID -- no se puede enviar el reporte")
        print(texto)
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={"chat_id": chat_id, "text": texto}, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram respondio {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Error enviando a Telegram: {e}")


def calcular_metricas(conn, desde: datetime, hasta: datetime) -> dict:
    """Toda la lectura es de solo consulta (SELECT) -- nunca escribe
    en la base de datos del motor."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

        # Decisiones totales y desglose por razon (para ver cuanto
        # bloquean los breakers, cuantas veces sizing=0, etc.)
        cur.execute("""
            SELECT razon, operar, COUNT(*) as n
            FROM decisiones
            WHERE ts >= %s AND ts < %s
            GROUP BY razon, operar
            ORDER BY n DESC
        """, (desde, hasta))
        desglose_decisiones = cur.fetchall()

        cur.execute("""
            SELECT COUNT(*) as total FROM decisiones WHERE ts >= %s AND ts < %s
        """, (desde, hasta))
        total_decisiones = cur.fetchone()["total"]

        # Trades reales cerrados en la ventana (shadow o real, se
        # reporta el shadow flag para que quede claro cual es cual)
        cur.execute("""
            SELECT ts_salida, pnl, razon, shadow
            FROM trades_live
            WHERE ts_salida >= %s AND ts_salida < %s
            ORDER BY ts_salida ASC
        """, (desde, hasta))
        trades = cur.fetchall()

        # Historico completo (desde siempre) para el acumulado real,
        # no solo la semana -- el walk-forward tiene sentido acumulado.
        cur.execute("""
            SELECT pnl, shadow FROM trades_live ORDER BY ts_salida ASC
        """)
        trades_historicos = cur.fetchall()

    return {
        "total_decisiones": total_decisiones,
        "desglose_decisiones": desglose_decisiones,
        "trades_semana": trades,
        "trades_historicos": trades_historicos,
    }


def formatear_reporte(m: dict, desde: datetime, hasta: datetime) -> str:
    trades_sem = m["trades_semana"]
    n_sem = len(trades_sem)
    ganadores_sem = [t for t in trades_sem if float(t["pnl"]) > 0]
    perdedores_sem = [t for t in trades_sem if float(t["pnl"]) <= 0]
    pnl_sem = sum(float(t["pnl"]) for t in trades_sem)
    wr_sem = (len(ganadores_sem) / n_sem * 100) if n_sem else None

    hist = m["trades_historicos"]
    n_hist = len(hist)
    ganadores_hist = [t for t in hist if float(t["pnl"]) > 0]
    perdedores_hist = [t for t in hist if float(t["pnl"]) <= 0]
    wr_hist = (len(ganadores_hist) / n_hist * 100) if n_hist else None
    pnl_hist = sum(float(t["pnl"]) for t in hist)

    ratio_hist = None
    if ganadores_hist and perdedores_hist:
        avg_g = sum(float(t["pnl"]) for t in ganadores_hist) / len(ganadores_hist)
        avg_p = abs(sum(float(t["pnl"]) for t in perdedores_hist) / len(perdedores_hist))
        if avg_p > 0:
            ratio_hist = avg_g / avg_p

    es_shadow = trades_sem[0]["shadow"] if trades_sem else (hist[0]["shadow"] if hist else True)
    modo = "SHADOW" if es_shadow else "LIVE"

    lineas = []
    lineas.append(f"📊 REPORTE SEMANAL KMT [{modo}]")
    lineas.append(f"{desde.strftime('%Y-%m-%d')} → {hasta.strftime('%Y-%m-%d')}")
    lineas.append("")
    lineas.append(f"Decisiones evaluadas: {m['total_decisiones']}")

    if m["desglose_decisiones"]:
        lineas.append("Desglose:")
        for d in m["desglose_decisiones"][:8]:
            estado = "✅ operado" if d["operar"] else "⛔ bloqueado"
            lineas.append(f"  · {d['razon']} ({estado}): {d['n']}")

    lineas.append("")
    lineas.append(f"Trades cerrados esta semana: {n_sem}")
    if n_sem:
        lineas.append(f"  Win rate semana: {wr_sem:.1f}%")
        lineas.append(f"  PnL semana: ${pnl_sem:.2f}")
    else:
        lineas.append("  (sin trades esta semana -- normal, la señal es de baja frecuencia)")

    lineas.append("")
    lineas.append(f"📈 Acumulado histórico ({n_hist} trades):")
    if n_hist:
        lineas.append(f"  Win rate acumulado: {wr_hist:.1f}%  (baseline backtest: {BASELINE['win_rate']}%)")
        if ratio_hist:
            lineas.append(f"  Ratio acumulado: {ratio_hist:.2f}  (baseline backtest: {BASELINE['ratio']})")
        lineas.append(f"  PnL acumulado: ${pnl_hist:.2f}")

        # Comparacion simple contra baseline, sin alarmismo -- solo el dato
        diff_wr = wr_hist - BASELINE["win_rate"]
        if abs(diff_wr) >= 15 and n_hist >= 10:
            lineas.append(f"  ⚠️ Win rate difiere {diff_wr:+.1f} pts del backtest -- vale la pena revisar régimen")
    else:
        lineas.append("  (aún sin trades reales registrados)")

    return "\n".join(lineas)


def main():
    db_url = os.environ.get("DATABASE_URL")
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not db_url:
        log.error("Falta DATABASE_URL -- no se puede generar el reporte")
        sys.exit(1)

    hasta = datetime.now(timezone.utc)
    desde = hasta - timedelta(days=7)

    conn = psycopg2.connect(db_url)
    conn.set_session(readonly=True)  # blindaje extra: esta conexion NUNCA escribe
    try:
        metricas = calcular_metricas(conn, desde, hasta)
    finally:
        conn.close()

    texto = formatear_reporte(metricas, desde, hasta)
    log.info("Reporte generado:\n" + texto)
    enviar_telegram(token, chat_id, texto)


if __name__ == "__main__":
    main()