"""Alertas por WhatsApp de comprobantes vencidos (Evolution API v2).

Uso:
    python alertas_vencimiento.py --atender-solicitudes   # atiende los envíos pedidos desde la app (tarea programada)
    python alertas_vencimiento.py --clientes              # avisa a cada cliente sus ventas que vencen hoy o ya vencieron
    python alertas_vencimiento.py --clientes --solo-hoy   # solo las ventas que vencen hoy
    python alertas_vencimiento.py --clientes --prueba     # muestra los mensajes en pantalla, no envía
    python alertas_vencimiento.py                         # avisa a WHATSAPP_DESTINO las compras que vencen hoy
    python alertas_vencimiento.py --prueba                # muestra el mensaje de compras, no envía
    python alertas_vencimiento.py --test-envio            # envía un mensaje fijo a WHATSAPP_DESTINO
"""
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import urllib3
from dotenv import load_dotenv

from database import get_db_connection

load_dotenv()

EVOLUTION_API_URL = os.getenv('EVOLUTION_API_URL', 'http://localhost:8080').rstrip('/')
EVOLUTION_API_KEY = os.getenv('EVOLUTION_API_KEY', '')
EVOLUTION_INSTANCE = os.getenv('EVOLUTION_INSTANCE', '')
WHATSAPP_DESTINO = os.getenv('WHATSAPP_DESTINO', '')
EVOLUTION_VERIFY_SSL = os.getenv('EVOLUTION_VERIFY_SSL', 'true').strip().lower() not in ('0', 'false', 'no')
APP_TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'America/Lima'))
EMPRESA_NOMBRE = os.getenv('EMPRESA_NOMBRE', 'Inversiones Horna Quintana E.I.R.L.')
PAUSA_ENTRE_ENVIOS = (6, 15)

TIPOS_COMPROBANTE = {
    'FACTURA': 'Factura',
    'BOLETA': 'Boleta',
    'NOTA_VENTA': 'Nota de venta',
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('alertas_vencimiento')

if not EVOLUTION_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _consultar(sql, params):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        columnas = [col[0].lower() for col in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]
    finally:
        conn.close()


def _fecha_txt(valor):
    return valor.strftime('%d/%m/%Y') if hasattr(valor, 'strftime') else str(valor or '')


def obtener_compras_vencen_hoy(hoy):
    """Compras activas con pago pendiente cuya FechaVencimiento es hoy."""
    inicio = datetime(hoy.year, hoy.month, hoy.day)
    return _consultar(
        """
        SELECT c.IdCompra, c.TipoComprobante, c.NroComprobanteRef,
               e.RazonSocial, c.Total, c.FechaCompra
        FROM dbo.Inventario_ComprasCab c
        INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = c.IdProveedor
        WHERE c.EstadoPago = 'PENDIENTE'
          AND c.EstadoCompra <> 'ANULADA'
          AND c.FechaVencimiento >= ? AND c.FechaVencimiento < ?
        ORDER BY e.RazonSocial, c.IdCompra
        """,
        (inicio, inicio + timedelta(days=1)),
    )


def obtener_ventas_por_cobrar(hoy, solo_hoy=False):
    """Ventas activas con pago pendiente que vencen hoy (y, si no es solo_hoy, las ya vencidas)."""
    inicio = datetime(hoy.year, hoy.month, hoy.day)
    desde = inicio if solo_hoy else datetime(1900, 1, 1)
    return _consultar(
        """
        SELECT v.IdVenta, v.IdCliente, v.TipoComprobante, v.NroComprobanteRef,
               v.FechaVenta, v.FechaVencimiento, v.Total, e.RazonSocial, e.Telefono
        FROM dbo.Inventario_VentasCab v
        INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = v.IdCliente
        WHERE v.EstadoPago = 'PENDIENTE'
          AND v.EstadoVenta <> 'ANULADA'
          AND v.FechaVencimiento >= ? AND v.FechaVencimiento < ?
        ORDER BY e.RazonSocial, v.FechaVencimiento, v.IdVenta
        """,
        (desde, inicio + timedelta(days=1)),
    )


def normalizar_telefono_peru(telefono):
    """Celular peruano en formato 51XXXXXXXXX, o None si no es válido."""
    digitos = re.sub(r'\D', '', str(telefono or ''))
    if len(digitos) == 9 and digitos.startswith('9'):
        return '51' + digitos
    if len(digitos) == 11 and digitos.startswith('519'):
        return digitos
    return None


def _bloque_compra(compra):
    tipo = TIPOS_COMPROBANTE.get(str(compra.get('tipocomprobante') or '').upper(), 'Documento')
    numero = (compra.get('nrocomprobanteref') or '').strip() or f"Compra N° {compra['idcompra']}"
    monto = float(compra.get('total') or 0)
    return (
        f"📄 *{tipo}:* {numero}\n"
        f"🏢 *Proveedor:* {compra.get('razonsocial') or ''}\n"
        f"💰 *Monto:* S/ {monto:,.2f}\n"
        f"📅 *Fecha Emisión:* {_fecha_txt(compra.get('fechacompra'))}"
    )


def armar_mensaje(compras):
    bloques = '\n\n'.join(_bloque_compra(c) for c in compras)
    return (
        "⚠️ *ALERTA DE VENCIMIENTO HOY* ⚠️\n\n"
        "Se detectaron las siguientes facturas de compras por pagar el día de hoy:\n\n"
        f"{bloques}\n\n"
        "Por favor coordinar el pago correspondiente."
    )


def _estado_vencimiento(fecha_venc, hoy):
    dias = (hoy - fecha_venc.date()).days if hasattr(fecha_venc, 'date') else 0
    if dias <= 0:
        return 'vence hoy'
    return 'venció ayer' if dias == 1 else f'vencido hace {dias} días'


def _bloque_venta(venta, hoy):
    tipo = TIPOS_COMPROBANTE.get(str(venta.get('tipocomprobante') or '').upper(), 'Comprobante')
    numero = (venta.get('nrocomprobanteref') or '').strip() or f"N° {venta['idventa']}"
    monto = float(venta.get('total') or 0)
    fecha_venc = venta.get('fechavencimiento')
    return (
        f"📄 *{tipo}:* {numero}\n"
        f"📅 *Fecha de emisión:* {_fecha_txt(venta.get('fechaventa'))}\n"
        f"⏰ *Vencimiento:* {_fecha_txt(fecha_venc)} ({_estado_vencimiento(fecha_venc, hoy)})\n"
        f"💰 *Monto:* S/ {monto:,.2f}"
    )


def armar_mensaje_cliente(ventas, hoy, empresa_nombre=EMPRESA_NOMBRE):
    cliente = str(ventas[0].get('razonsocial') or '').strip()
    bloques = '\n\n'.join(_bloque_venta(v, hoy) for v in ventas)
    if len(ventas) == 1:
        intro = 'le recordamos que tiene el siguiente comprobante pendiente de pago:'
        total_txt = ''
    else:
        intro = 'le recordamos que tiene los siguientes comprobantes pendientes de pago:'
        total = sum(float(v.get('total') or 0) for v in ventas)
        total_txt = f"\n\n💵 *Total pendiente:* S/ {total:,.2f}"
    return (
        f"Hola *{cliente}* 👋\n\n"
        f"Le saludamos de *{empresa_nombre}*, {intro}\n\n"
        f"{bloques}{total_txt}\n\n"
        "Si ya realizó el pago, por favor ignore este mensaje. ¡Gracias por su preferencia!"
    )


def enviar_whatsapp(mensaje, numero_destino):
    """Envía un texto por Evolution API v2. Retorna True si se envió."""
    if not (EVOLUTION_API_KEY and EVOLUTION_INSTANCE and numero_destino):
        log.error('Falta configurar EVOLUTION_API_KEY, EVOLUTION_INSTANCE o el número de destino')
        return False
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    headers = {'apikey': EVOLUTION_API_KEY, 'Content-Type': 'application/json'}
    payload = {'number': numero_destino, 'text': mensaje}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20, verify=EVOLUTION_VERIFY_SSL)
        resp.raise_for_status()
        log.info('Mensaje enviado a %s', numero_destino)
        return True
    except requests.RequestException as e:
        detalle = e.response.text[:300] if getattr(e, 'response', None) is not None else ''
        log.error('No se pudo enviar el WhatsApp a %s: %s %s', numero_destino, e, detalle)
        return False


def alertar_clientes(hoy, solo_hoy, solo_mostrar, empresa_nombre=EMPRESA_NOMBRE):
    """Envía un mensaje por cliente con sus ventas por cobrar. Retorna un resumen del envío."""
    resumen = {'clientes': 0, 'enviados': 0, 'fallidos': 0, 'sin_celular': 0, 'detalle': []}
    ventas = obtener_ventas_por_cobrar(hoy, solo_hoy)
    if not ventas:
        log.info('Sin ventas pendientes por cobrar al %s.', _fecha_txt(hoy))
        resumen['detalle'].append(f'No hay ventas pendientes que venzan el {_fecha_txt(hoy)}.')
        return resumen

    por_cliente = {}
    for v in ventas:
        por_cliente.setdefault(v['idcliente'], []).append(v)
    resumen['clientes'] = len(por_cliente)

    for ventas_cliente in por_cliente.values():
        cliente = str(ventas_cliente[0].get('razonsocial') or '').strip()
        numero = normalizar_telefono_peru(ventas_cliente[0].get('telefono'))
        if not numero:
            resumen['sin_celular'] += 1
            resumen['detalle'].append(f'Sin celular: {cliente}')
            log.warning('%s: sin celular válido en su ficha (%s). No se envía.',
                        cliente, ventas_cliente[0].get('telefono') or 'vacío')
            continue

        mensaje = armar_mensaje_cliente(ventas_cliente, hoy, empresa_nombre)
        if solo_mostrar:
            print(f"\n----- {cliente} -> {numero} -----\n{mensaje}")
            continue

        if resumen['enviados'] + resumen['fallidos']:
            time.sleep(random.uniform(*PAUSA_ENTRE_ENVIOS))
        if enviar_whatsapp(mensaje, numero):
            resumen['enviados'] += 1
            resumen['detalle'].append(f'Enviado: {cliente} ({numero}), {len(ventas_cliente)} comprobante(s)')
        else:
            resumen['fallidos'] += 1
            resumen['detalle'].append(f'Falló: {cliente} ({numero})')

    log.info('Clientes con deuda: %d | enviados: %d | fallidos: %d | sin celular: %d',
             resumen['clientes'], resumen['enviados'], resumen['fallidos'], resumen['sin_celular'])
    return resumen


def obtener_empresa(company):
    filas = _consultar(
        "SELECT Company, Description, Telephone FROM dbo.SY_Company WHERE Company = ?",
        (company,),
    )
    return filas[0] if filas else None


def verificar_emisor(telefono_empresa):
    """Comprueba que la instancia de Evolution esté conectada con el número emisor configurado.
    Retorna None si todo está bien, o el texto del problema."""
    esperado = normalizar_telefono_peru(telefono_empresa)
    if not esperado:
        return 'La empresa no tiene un teléfono emisor válido configurado.'
    try:
        resp = requests.get(
            f"{EVOLUTION_API_URL}/instance/fetchInstances",
            params={'instanceName': EVOLUTION_INSTANCE},
            headers={'apikey': EVOLUTION_API_KEY},
            timeout=20,
            verify=EVOLUTION_VERIFY_SSL,
        )
        resp.raise_for_status()
        datos = resp.json()
    except (requests.ConnectionError, requests.Timeout) as e:
        log.error('Evolution API no responde: %s', e)
        return 'No se pudo conectar con Evolution API. Verifique que el servicio EvolutionAPI esté iniciado en el servidor.'
    except requests.HTTPError as e:
        log.error('Evolution API respondió con error: %s %s', e, resp.text[:300])
        if resp.status_code in (401, 403):
            return 'Evolution API rechazó la API key. Revise EVOLUTION_API_KEY en el .env del servidor.'
        if resp.status_code == 404:
            return f'No existe la instancia "{EVOLUTION_INSTANCE}" en Evolution API.'
        return f'Evolution API respondió con error {resp.status_code}.'
    except (requests.RequestException, ValueError) as e:
        log.error('Respuesta inválida de Evolution API: %s', e)
        return 'Respuesta inválida de Evolution API.'

    instancias = datos if isinstance(datos, list) else [datos]
    instancia = next(
        (i for i in instancias if isinstance(i, dict)
         and (i.get('name') or (i.get('instance') or {}).get('instanceName')) == EVOLUTION_INSTANCE),
        instancias[0] if instancias and isinstance(instancias[0], dict) else {},
    )
    detalle = instancia.get('instance') if isinstance(instancia.get('instance'), dict) else instancia
    estado = str(detalle.get('connectionStatus') or detalle.get('status') or '').lower()
    owner = str(detalle.get('ownerJid') or detalle.get('owner') or '')
    if estado and estado != 'open':
        return f'El WhatsApp emisor está desconectado (estado: {estado}). Vuelva a escanear el QR en Evolution.'
    numero_conectado = re.sub(r'\D', '', owner.split('@')[0])
    if numero_conectado and numero_conectado != esperado:
        return (f'El WhatsApp conectado en Evolution es {numero_conectado}, '
                f'pero el teléfono emisor configurado es {esperado}.')
    if not numero_conectado:
        log.warning('No se pudo leer el número conectado en Evolution; se continúa sin verificarlo.')
    return None


def _ejecutar_sql(sql, params):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        filas = cursor.fetchall() if cursor.description else []
        conn.commit()
        return filas
    finally:
        conn.close()


def atender_solicitudes():
    """Procesa las solicitudes PENDIENTE registradas desde la app."""
    ahora = lambda: datetime.now(APP_TZ).replace(tzinfo=None)
    _ejecutar_sql(
        """
        UPDATE dbo.Inventario_AlertasSolicitud
        SET Estado = 'ERROR', FechaFin = ?, Resultado = 'Envío interrumpido: el proceso no terminó.'
        WHERE Estado = 'PROCESANDO' AND FechaInicio < ?
        """,
        (ahora(), ahora() - timedelta(minutes=30)),
    )

    while True:
        filas = _ejecutar_sql(
            """
            ;WITH s AS (
                SELECT TOP 1 * FROM dbo.Inventario_AlertasSolicitud WITH (ROWLOCK, UPDLOCK, READPAST)
                WHERE Estado = 'PENDIENTE'
                ORDER BY IdSolicitud
            )
            UPDATE s SET Estado = 'PROCESANDO', FechaInicio = ?
            OUTPUT INSERTED.IdSolicitud, INSERTED.Company, INSERTED.Tipo
            """,
            (ahora(),),
        )
        if not filas:
            return 0
        id_solicitud, company, tipo = filas[0][0], str(filas[0][1]).strip(), str(filas[0][2]).strip()
        log.info('Atendiendo solicitud %s (%s) de la empresa %s', id_solicitud, tipo, company)

        resumen = {'enviados': 0, 'fallidos': 0, 'sin_celular': 0, 'detalle': []}
        estado = 'ATENDIDA'
        try:
            empresa = obtener_empresa(company)
            problema = 'No se encontró la empresa.' if not empresa else verificar_emisor(empresa.get('telephone'))
            if problema:
                estado = 'ERROR'
                resumen['detalle'].append(problema)
                log.error('Solicitud %s: %s', id_solicitud, problema)
            else:
                nombre = str(empresa.get('description') or '').strip() or EMPRESA_NOMBRE
                resumen = alertar_clientes(datetime.now(APP_TZ).date(), True, False, nombre)
                if resumen['fallidos'] and not resumen['enviados']:
                    estado = 'ERROR'
        except Exception as e:
            log.exception('Solicitud %s falló', id_solicitud)
            estado = 'ERROR'
            resumen['detalle'].append(f'Error inesperado: {e}')

        _ejecutar_sql(
            """
            UPDATE dbo.Inventario_AlertasSolicitud
            SET Estado = ?, FechaFin = ?, Enviados = ?, Fallidos = ?, SinCelular = ?, Resultado = ?
            WHERE IdSolicitud = ?
            """,
            (estado, ahora(), resumen['enviados'], resumen['fallidos'], resumen['sin_celular'],
             '\n'.join(resumen['detalle'])[:4000], id_solicitud),
        )


def main():
    argumentos = set(sys.argv[1:])
    hoy = datetime.now(APP_TZ).date()
    solo_mostrar = '--prueba' in argumentos
    if solo_mostrar:
        sys.stdout.reconfigure(encoding='utf-8')

    if '--test-envio' in argumentos:
        mensaje = (
            "✅ *Prueba de alertas* — Sistema de Inventario\n\n"
            f"Mensaje de prueba enviado el {datetime.now(APP_TZ).strftime('%d/%m/%Y %H:%M')}."
        )
        return 0 if enviar_whatsapp(mensaje, WHATSAPP_DESTINO) else 1

    if '--atender-solicitudes' in argumentos:
        return atender_solicitudes()

    if '--clientes' in argumentos:
        resumen = alertar_clientes(hoy, '--solo-hoy' in argumentos, solo_mostrar)
        return 1 if resumen['fallidos'] else 0

    compras = obtener_compras_vencen_hoy(hoy)
    if not compras:
        log.info('Sin comprobantes que venzan hoy (%s). No se envía alerta.', _fecha_txt(hoy))
        return 0

    mensaje = armar_mensaje(compras)
    log.info('%d comprobante(s) vencen hoy.', len(compras))
    if solo_mostrar:
        print(mensaje)
        return 0
    return 0 if enviar_whatsapp(mensaje, WHATSAPP_DESTINO) else 1


if __name__ == '__main__':
    sys.exit(main())
