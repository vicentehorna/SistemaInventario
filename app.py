import json
import os
import re
import sys
import logging
import io
import zipfile
import base64
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal

import resend
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response, send_file, has_request_context, stream_with_context
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from dotenv import load_dotenv

# --- CONFIGURACIÓN FORZADA DE GTK3 ---
# Verifica que esta sea la ruta real tras la instalación
gtk_path = r'C:\Program Files\GTK3-Runtime Win64\bin'

if os.path.exists(gtk_path):
    # Agregamos al PATH de Windows
    os.environ['PATH'] = gtk_path + os.pathsep + os.environ.get('PATH', '')
    # Necesario para Python 3.8+ en Windows
    if hasattr(os, 'add_dll_directory'):
        try:
            os.add_dll_directory(gtk_path)
        except Exception:
            pass
# -------------------------------------

try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception as _weasy_err:
    HTML = None
    WEASYPRINT_AVAILABLE = False
    _WEASYPRINT_IMPORT_ERROR = _weasy_err

from database import (
    User,
    get_datos_usuario_web,
    get_logoweb_empresa,
    cambiar_password,
    get_db_connection,
    get_config_empresa,
    get_listado_generar_boletas,
    insertar_documento_minero,
    get_ruta_documentos_usuario,
    sincronizar_metadata_drive,
    sincronizar_metadata_drive_lote,
    ejecutar_sp_updatecompany_documentos_boletas,
    actualizar_fechadescarga_boleta,
    update_ruta_documentos_usuario,
    get_tipos_documentos,
    get_historial_solicitud_vacaciones,
    get_rangos_solicitud_vacaciones,
    registrar_solicitud_vacaciones,
    eliminar_solicitud_vacaciones,
    aprobar_solicitud_vacaciones_con_sustento,
    obtener_sustento_drive_ids_por_solicitudes,
    obtener_drive_file_id_sustento_vacaciones,
    solicitud_vacaciones_tiene_cruce,
    get_resumen_solicitud_vacaciones,
    get_max_dias_vacaciones,
    get_inventario_categorias,
    get_inventario_marcas,
    insertar_inventario_item,
    get_listado_articulos_inventario,
    get_inventario_item_por_id,
    actualizar_inventario_item,
    eliminar_inventario_item,
    get_ubigeo_departamentos,
    get_ubigeo_provincias,
    get_ubigeo_distritos,
    insertar_inventario_empresa,
    get_inventario_empresa_por_id,
    actualizar_inventario_empresa,
    eliminar_inventario_empresa,
    get_listado_empresas_inventario,
    get_proveedores_activos,
    get_articulos_para_compra,
    get_lista_compras_inventario,
    insertar_compra,
    get_compra_por_id,
    actualizar_compra,
    anular_compra,
    get_fecha_hoy_sql,
    get_clientes_activos,
    get_articulos_para_venta,
    insertar_venta,
    get_lista_ventas_inventario,
    anular_venta,
    get_venta_por_id,
    actualizar_venta,
)

load_dotenv()

_APP_TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'America/Lima'))


def _fecha_hoy_app():
    """Fecha calendario: prioriza GETDATE() de SQL Server, luego zona Lima."""
    fecha_sql = get_fecha_hoy_sql()
    if fecha_sql:
        return fecha_sql
    return datetime.now(_APP_TZ).date()


app = Flask(__name__)
# Credenciales Google Drive: use GOOGLE_DRIVE_CREDENTIALS_FILE o SERVICE_ACCOUNT_FILE en .env (ruta al JSON).
SERVICE_ACCOUNT_FILE = os.getenv('SERVICE_ACCOUNT_FILE')
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-key-123')

logging.getLogger('werkzeug').setLevel(logging.ERROR)
sys.stdout.reconfigure(line_buffering=True)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

LOGO_INVENTARIO_FILE = 'LogoInventario.png'
LOGO_EMPRESA_DEFAULT = 'logo_default.jpg'
LOGOS_EMPRESA_DIR = os.path.join('static', 'img', 'logos')


def _company_for_logo_session():
    """Compañía efectiva para branding (respeta bloqueos MINERO/SIMPLE)."""
    if session.get('simple_profile') and session.get('simple_lock_company'):
        return str(session['simple_lock_company']).strip()
    if session.get('minero_profile') and session.get('minero_lock_company'):
        return str(session['minero_lock_company']).strip()
    return str(session.get('company') or '').strip()


def _sync_session_logoweb():
    """Persiste en sesión el nombre de archivo logoweb de la compañía del usuario."""
    cia = _company_for_logo_session()
    if not cia:
        session.pop('logoweb', None)
        return
    session['logoweb'] = get_logoweb_empresa(cia) or ''


def _resolver_logo_empresa_url():
    """Logo fijo del sistema de inventario (mismo para todos los usuarios)."""
    logos_root = os.path.join(app.root_path, LOGOS_EMPRESA_DIR)
    for name in (LOGO_INVENTARIO_FILE, LOGO_EMPRESA_DEFAULT, 'logo_default.png'):
        if name and os.path.isfile(os.path.join(logos_root, name)):
            return url_for('static', filename=f'img/logos/{name}')
    return url_for('static', filename=f'img/logos/{LOGO_EMPRESA_DEFAULT}')


def ensure_user_session():
    """Asegura que company y person estén en sesión y actualiza alcance MINERO/SIMPLE (documentos)."""
    if not current_user.is_authenticated:
        return {'company': session.get('company'), 'person': session.get('person')}
    if User.is_temp_user(current_user.get_id()):
        session['general_profile'] = True
        session['minero_profile'] = False
        session['simple_profile'] = False
        return {
            'company': session.get('company'),
            'person': session.get('person'),
            'minero_profile': False,
            'minero_lock_company': None,
            'simple_profile': False,
            'simple_lock_company': None,
            'simple_lock_person': None,
        }
    cu_company = str(getattr(current_user, 'company', None) or '').strip()
    cu_person = str(getattr(current_user, 'person', None) or '').strip()
    if cu_company:
        session['company'] = cu_company
    if cu_person:
        session['person'] = cu_person
    if not session.get('company') or not session.get('person'):
        if User.is_temp_user(current_user.get_id()):
            info = None
        else:
            info = get_datos_usuario_web(current_user.id)
        if info:
            session['company'], session['person'] = info['company'], info['person']
            if info.get('logoweb') is not None:
                session['logoweb'] = str(info.get('logoweb') or '').strip()
    _refresh_documentos_alcance_session()
    _sync_session_logoweb()
    return {
        'company': session.get('company'),
        'person': session.get('person'),
        'minero_profile': session.get('minero_profile'),
        'minero_lock_company': session.get('minero_lock_company'),
        'simple_profile': session.get('simple_profile'),
        'simple_lock_company': session.get('simple_lock_company'),
        'simple_lock_person': session.get('simple_lock_person'),
    }


def _refresh_documentos_alcance_session():
    """Fija en sesión compañía (MINERO/SIMPLE) y trabajador (SIMPLE) para Documentos del personal."""
    if not current_user.is_authenticated:
        return
    if User.is_temp_user(current_user.get_id()):
        session['documentos_alcance_uid'] = str(current_user.get_id())
        session['general_profile'] = True
        session['minero_profile'] = False
        session['simple_profile'] = False
        return
    uid = str(current_user.get_id())
    if session.get('documentos_alcance_uid') == uid:
        return
    session['documentos_alcance_uid'] = uid
    session.pop('minero_lock_company', None)
    session['minero_profile'] = False
    session.pop('simple_lock_company', None)
    session.pop('simple_lock_person', None)
    session.pop('simple_lock_person_name', None)
    session['simple_profile'] = False
    session['general_profile'] = False

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        session['general_profile'] = User._tiene_perfil_general(cursor, current_user.id)
        cursor.close()
        conn.close()
    except Exception:
        logging.exception('_refresh_documentos_alcance_session general_profile')

    lock_minero = User.get_minero_lock_company(current_user.id)
    if lock_minero:
        session['minero_lock_company'] = str(lock_minero).strip()
        session['minero_profile'] = True

    scope_simple = User.get_simple_documentos_scope(current_user.id)
    if scope_simple:
        session['simple_profile'] = True
        session['simple_lock_company'] = str(scope_simple['company']).strip()
        session['simple_lock_person'] = str(scope_simple['person']).strip()
        session['simple_lock_person_name'] = str(scope_simple.get('person_name') or '').strip()


def _refresh_minero_scope_session():
    """Compatibilidad: delega en _refresh_documentos_alcance_session."""
    _refresh_documentos_alcance_session()


def _documentos_effective_company_lock():
    """Compañía fija para MINERO o SIMPLE en Documentos del personal."""
    if session.get('simple_profile') and session.get('simple_lock_company'):
        return str(session['simple_lock_company']).strip()
    if session.get('minero_profile') and session.get('minero_lock_company'):
        return str(session['minero_lock_company']).strip()
    return None


def _minero_effective_company_lock():
    """Código de compañía fija para usuarios MINERO (otros reportes), o None."""
    if session.get('minero_profile') and session.get('minero_lock_company'):
        return str(session['minero_lock_company']).strip()
    return None


def _documentos_effective_person_lock():
    """Código de trabajador fijo para perfil SIMPLE en Documentos del personal."""
    if session.get('simple_profile') and session.get('simple_lock_person'):
        return str(session['simple_lock_person']).strip()
    return None


def _descripcion_compania_selector(codigo):
    """Texto del combo compañías (sp_pr_selectorcompanias_web) para un código dado."""
    codigo = str(codigo or '').strip()
    if not codigo:
        return ''
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('EXEC sp_pr_selectorcompanias_web')
        rows = cursor.fetchall()
        for r in rows:
            if str(getattr(r, 'Company', r[0])).strip() == codigo:
                desc = getattr(r, 'description', None)
                if desc is None and len(r) > 1:
                    desc = r[1]
                return str(desc).strip() if desc is not None else codigo
    except Exception:
        logging.exception('_descripcion_compania_selector')
        return codigo
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return codigo


def _minero_reporte_template_context():
    """Contexto Jinja común para reportes con compañía fija (perfil MINERO)."""
    ensure_user_session()
    lock = _minero_effective_company_lock()
    return {
        'minero_restringe_compania': bool(lock),
        'minero_compania_codigo': lock or '',
        'minero_compania_text': _descripcion_compania_selector(lock) if lock else '',
    }


def _usuario_perfil_general_o_minero():
    """True si el usuario tiene perfil GENERAL o MINERO."""
    ensure_user_session()
    return bool(session.get('general_profile') or session.get('minero_profile'))


def _reporte_compania_usuario_logueado():
    """Compañía del usuario logueado (bloqueo MINERO o company de sesión/login)."""
    lock = _minero_effective_company_lock()
    if lock:
        return lock
    return str(getattr(current_user, 'company', None) or session.get('company') or '').strip()


def _reporte_aprobar_vacaciones_template_context():
    """Filtros del reporte Aprobar Vacaciones: compañía fija del usuario logueado."""
    ensure_user_session()
    cia = _reporte_compania_usuario_logueado()
    return {
        'minero_restringe_compania': True,
        'minero_compania_codigo': cia,
        'minero_compania_text': _descripcion_compania_selector(cia) if cia else cia,
        'simple_restringe_trabajador': False,
        'simple_trabajador_codigo': '',
        'simple_trabajador_nombre': '',
    }


def _status_vacaciones_reporte_text(status_code):
    code = str(status_code or '').strip().upper()
    if code == 'A':
        return 'Aprobado'
    if code == 'R':
        return 'Rechazado'
    if code == 'P':
        return 'Pendiente'
    return str(status_code or '').strip()


def _reporte_filtros_perfil_template_context():
    """MINERO/SIMPLE fijan compañía; SIMPLE fija trabajador (documentos, vacaciones, saldo)."""
    ensure_user_session()
    lock_cia = _documentos_effective_company_lock()
    lock_person = _documentos_effective_person_lock()
    person_name = str(session.get('simple_lock_person_name') or '').strip()
    if lock_person and not person_name:
        person_name = lock_person
    return {
        'minero_restringe_compania': bool(lock_cia),
        'minero_compania_codigo': lock_cia or '',
        'minero_compania_text': _descripcion_compania_selector(lock_cia) if lock_cia else '',
        'simple_restringe_trabajador': bool(lock_person),
        'simple_trabajador_codigo': lock_person or '',
        'simple_trabajador_nombre': person_name,
    }


def _documentos_personal_template_context():
    return _reporte_filtros_perfil_template_context()


def _tipos_documento_web_catalog():
    """Lista PR_tipodocWeb con metadatos para tarjetas del dashboard SIMPLE."""
    catalog = []
    for row in get_tipos_documentos() or []:
        if not isinstance(row, dict):
            continue
        codigo = str(row.get('Tipodocumento') or '').strip()
        if not codigo:
            continue
        nombre = str(row.get('name') or codigo).strip()
        meta = _meta_tipo_documento_web(codigo, nombre)
        catalog.append({
            'codigo': codigo,
            'nombre': nombre,
            'icono': meta['icono'],
            'descripcion': meta['descripcion'],
        })
    return catalog


def _meta_tipo_documento_web(codigo, nombre):
    """Icono y texto corto por tipo de documento (dashboard SIMPLE)."""
    cod = str(codigo or '').strip().upper()
    nombre = str(nombre or cod).strip()
    presets = {
        'BOL': ('bi-file-earmark-text', 'Descarga tus boletas de pago'),
        'CTS': ('bi-piggy-bank', 'Certificados y liquidaciones CTS'),
        'GRA': ('bi-mortarboard', 'Constancias y documentos de gratificación'),
        'VAC': ('bi-calendar-check', 'Documentos relacionados a vacaciones'),
        'UTI': ('bi-calculator', 'Utilidades y participaciones'),
        'LIQ': ('bi-cash-stack', 'Liquidaciones y finiquitos'),
        'PLA': ('bi-clipboard-data', 'Planillas y resúmenes'),
    }
    icono, descripcion = presets.get(cod, ('bi-file-earmark-pdf', f'Descarga tus documentos de {nombre}'))
    return {'icono': icono, 'descripcion': descripcion}


def _tipodoc_web_por_codigo(codigo):
    codigo = str(codigo or '').strip()
    if not codigo:
        return None
    for item in _tipos_documento_web_catalog():
        if item['codigo'] == codigo:
            return item
    return None


def _documentos_personal_fetch_rows(cia, period, tipodoc, person, dni):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_reportenotificaciones_web @cia=?, @period=?, @tipodoc=?, @person=?, @dni=?",
            (cia, period, tipodoc, person, dni),
        )
        return _dicts_first_nonempty_resultset(cursor)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _documentos_personal_build_payload(rows, cia, modo='completo'):
    """Arma headers/data JSON del reporte de documentos (completo o SIMPLE/móvil)."""
    if modo == 'simple':
        headers_es = ['Periodo', 'Fecha descarga', 'Descargar']
        resultado = []
        for r in rows:
            periodo_doc = _fmt_periodo_yyyy_mm(r.get('periodo'))
            drive_id = str(r.get('drivefileid') or '').strip()
            tipo_doc = _jsonable_value(r.get('tipodocumento'))
            dni = str(r.get('person') or '').strip()
            resultado.append([
                periodo_doc,
                _fmt_fecha_hora_dd_mm_yyyy_hh_mm(r.get('fechadescarga')),
                {
                    'drivefileid': drive_id,
                    'person': dni,
                    'period': str(r.get('periodo') or '').strip(),
                    'tipodocumento': str(tipo_doc or '').strip(),
                    'cia': cia,
                },
            ])
        return {'headers': headers_es, 'data': resultado}

    headers_es = [
        'Código',
        'Nombre',
        'Tipo documento',
        'Periodo',
        'Fecha descarga',
        'Descargar',
    ]
    resultado = []
    for r in rows:
        periodo_doc = _fmt_periodo_yyyy_mm(r.get('periodo'))
        drive_id = str(r.get('drivefileid') or '').strip()
        tipo_doc = _jsonable_value(r.get('tipodocumento'))
        dni = str(r.get('person') or '').strip()
        resultado.append([
            _jsonable_value(r.get('person')),
            _jsonable_value(r.get('name')),
            tipo_doc,
            periodo_doc,
            _fmt_fecha_hora_dd_mm_yyyy_hh_mm(r.get('fechadescarga')),
            {
                'drivefileid': drive_id,
                'person': dni,
                'period': str(r.get('periodo') or '').strip(),
                'tipodocumento': str(tipo_doc or '').strip(),
                'cia': cia,
            },
        ])
    return {'headers': headers_es, 'data': resultado}


def _url_inicio_portal():
    """Página de inicio tras login."""
    return url_for('dashboard')


def _documentos_personal_redirect_tras_descarga():
    if session.get('simple_profile'):
        tipodoc = str(request.args.get('tipodocumento') or '').strip()
        if tipodoc and _tipodoc_web_por_codigo(tipodoc):
            return redirect(url_for('mis_documentos_simple_page', tipodoc=tipodoc))
        return redirect(url_for('dashboard'))
    return redirect(url_for('reporte_documentos_personal_page'))


@app.template_filter('importe')
def format_importe(value):
    try:
        return '{:,.2f}'.format(float(value or 0))
    except Exception:
        return '0.00'


@app.template_filter('pct')
def format_pct(value):
    try:
        return '{:.2f} %'.format(float(value or 0))
    except Exception:
        return '0.00 %'


@app.template_filter('fecha')
def fecha_filter(value):
    if not value:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y')
    if isinstance(value, date):
        return value.strftime('%d/%m/%Y')
    s = str(value).strip()
    if not s:
        return ''
    try:
        if len(s) >= 10 and s[4] == '-' and s[7] == '-':
            return datetime.strptime(s[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
    except Exception:
        pass
    return s


@app.before_request
def _preload_user_session():
    """Sincroniza company/logoweb antes de renderizar plantillas (p. ej. dashboard)."""
    if request.endpoint == 'static':
        return
    if current_user.is_authenticated:
        ensure_user_session()


@app.context_processor
def inject_now():
    return {
        'now': datetime.now(),
        'logo_empresa': _resolver_logo_empresa_url(),
    }


def _jsonable_value(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y')
    if isinstance(value, date):
        return value.strftime('%d/%m/%Y')
    return value


def _report_column_name(name):
    """La primera columna del SP no tiene alias; pyodbc puede devolver '' → periodo_fmt."""
    if name is None:
        return 'periodo_fmt'
    if isinstance(name, str) and not name.strip():
        return 'periodo_fmt'
    return name


def _normalize_pr_period(period_raw):
    """
    PRPeriod en BD es yyyymmdd (8 dígitos), p. ej. 20251212.
    Acepta también '2025-12-12' o '2025/12/12' por si el valor llegó formateado.
    """
    s = str(period_raw or '').strip().replace('-', '').replace('/', '')
    if len(s) >= 8 and s[:8].isdigit():
        return s[:8]
    return str(period_raw or '').strip()


def _fmt_periodo_yyyy_mm(val):
    """Periodo para columnas de reporte: YYYY-MM (p. ej. 20250301 → 2025-03)."""
    if val is None:
        return ''
    s = re.sub(r'\D', '', str(val).strip())
    if len(s) >= 6:
        return f'{s[:4]}-{s[4:6]}'
    return str(val).strip()


def _fmt_fecha_hora_dd_mm_yyyy_hh_mm(val):
    """
    Fecha y hora para columnas de reporte (p. ej. Fecha descarga): dd/mm/yyyy HH:MM.
    Acepta datetime, date o cadenas típicas de SQL/pyodbc.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.strftime('%d/%m/%Y %H:%M')
    if isinstance(val, date):
        return val.strftime('%d/%m/%Y')
    s = str(val).strip()
    if not s:
        return None
    try:
        if 'T' in s:
            norm = s.replace('Z', '+00:00') if s.endswith('Z') else s
            dt = datetime.fromisoformat(norm)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt.strftime('%d/%m/%Y %H:%M')
    except Exception:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s[:26], fmt).strftime('%d/%m/%Y %H:%M')
        except ValueError:
            continue
    try:
        if len(s) >= 10 and s[4] == '-' and s[7] == '-':
            return datetime.strptime(s[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
    except ValueError:
        pass
    return s


def _status_solicitud_vacaciones_badge(status_code):
    code = str(status_code or '').strip().upper()
    if code == 'A':
        return {
            'text': 'Aprobado',
            'class': 'bg-success-subtle text-success border border-success-subtle',
        }
    if code == 'R':
        return {
            'text': 'Rechazado',
            'class': 'bg-danger-subtle text-danger border border-danger-subtle',
        }
    return {
        'text': 'Pendiente',
        'class': 'bg-warning-subtle text-warning border border-warning-subtle',
    }


def _dias_totales_vacaciones_ejercicio(company):
    """Días de vacaciones asignados por ejercicio (PR_mapping2.DiasVacaciones)."""
    if not company:
        return 30
    return get_max_dias_vacaciones(company) or 30


def _historial_solicitud_vacaciones_items(rows, format_dates=False):
    """Filas de historial para plantilla o API JSON."""
    historial = []
    for r in rows:
        status_code = str(r.get('status') or '').strip().upper() or 'P'
        date_begin = r.get('DateBegin')
        date_end = r.get('DateEnd')
        historial.append({
            'id': r.get('Id'),
            'date_begin': fecha_filter(date_begin) if format_dates else date_begin,
            'date_end': fecha_filter(date_end) if format_dates else date_end,
            'days': int(r.get('Days') or 0),
            'can_delete': status_code == 'P',
            'control_year': str(r.get('ControlYear') or '').strip(),
        })
    return historial


def _days_between_calendar(fecha_inicio_str, fecha_fin_str):
    """Cantidad de días calendario inclusivos entre dos fechas ISO (YYYY-MM-DD)."""
    try:
        d1 = datetime.strptime(str(fecha_inicio_str).strip(), '%Y-%m-%d').date()
        d2 = datetime.strptime(str(fecha_fin_str).strip(), '%Y-%m-%d').date()
    except Exception:
        return 0
    if d2 < d1:
        return 0
    return (d2 - d1).days + 1


def _report_params_from_json(req):
    """Extrae y normaliza los 4 parámetros del SP (mismo orden que SSMS)."""
    body = req.get_json(silent=True) or {}
    cia = str(body.get('cia') or '').strip()
    payrolltype = str(body.get('payrolltype') or '').strip()
    period = _normalize_pr_period(body.get('period'))
    person = str(body.get('person') or '').strip()
    if not (cia and payrolltype and period and person):
        return None
    return (cia, payrolltype, period, person)


def _fetch_first_nonempty_resultset(cursor):
    """
    Algunos SP devuelven resultsets vacíos antes del SELECT final;
    avanza con nextset() hasta encontrar filas (o se acaban los sets).
    """
    columns = []
    rows = []
    while True:
        if cursor.description:
            columns = [_report_column_name(c[0]) for c in cursor.description]
            rows = cursor.fetchall()
            if rows:
                return columns, rows
        if not cursor.nextset():
            break
    return columns, []


def _dicts_first_nonempty_resultset(cursor):
    """
    Igual que _fetch_first_nonempty_resultset pero devuelve filas como dicts
    con claves en minúsculas (robusto con pyodbc / alias del SP).
    """
    while True:
        if cursor.description:
            cols = [str(c[0]).strip() for c in cursor.description]
            rows = cursor.fetchall()
            if rows:
                out = []
                for row in rows:
                    rd = {}
                    for i, cname in enumerate(cols):
                        key = (cname or f"col{i}").lower()
                        rd[key] = row[i]
                    out.append(rd)
                return out
        if not cursor.nextset():
            break
    return []


def _dicts_last_nonempty_resultset(cursor):
    """
    Como _dicts_first_nonempty_resultset pero devuelve el último result set con filas.
    Útil cuando un SP ejecuta DML y luego un SELECT final.
    """
    last_out = []
    while True:
        if cursor.description:
            cols = [str(c[0]).strip() for c in cursor.description]
            rows = cursor.fetchall()
            if rows:
                out = []
                for row in rows:
                    rd = {}
                    for i, cname in enumerate(cols):
                        key = (cname or f"col{i}").lower()
                        rd[key] = row[i]
                    out.append(rd)
                last_out = out
        if not cursor.nextset():
            break
    return last_out


def _sanitize_dynamic_procedure_name(name):
    """
    Valida ProcedureName leído de PR_ProcessType antes de usarlo en {{CALL ...}}.
    Permite esquema.procedimiento (segmentos alfanuméricos / guión bajo).
    """
    s = str(name or "").strip()
    if not s or len(s) > 200 or ".." in s:
        return None
    for part in s.split("."):
        if not part or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part):
            return None
    return s


def _drain_pyodbc_cursor(cursor):
    """Consume resultsets pendientes tras EXEC/CALL (evita errores en la siguiente ejecución)."""
    try:
        while True:
            if cursor.description:
                cursor.fetchall()
            if not cursor.nextset():
                break
    except Exception:
        logging.debug("drenado de cursor", exc_info=True)


def _is_comm_link_failure(err):
    """Detecta caídas transitorias de enlace ODBC/SQL Server."""
    s = str(err or "").lower()
    return ("08s01" in s) or ("communication link failure" in s)


def _is_transient_sql_error(err):
    """Errores reintentables: enlace caído o timeout de comando."""
    s = str(err or "").lower()
    return _is_comm_link_failure(err) or ("hyt00" in s) or ("timeout expired" in s)


def _sql_call_timeout_seconds():
    raw = str(os.getenv("SQL_CALL_TIMEOUT_SEC", "35")).strip()
    try:
        n = int(raw)
    except Exception:
        n = 35
    return max(10, min(n, 180))


def _set_cursor_timeout(cursor):
    """Timeout por ejecución de SP (segundos) para evitar cuelgues largos."""
    try:
        cursor.timeout = _sql_call_timeout_seconds()
    except Exception:
        logging.debug("No se pudo fijar timeout en cursor", exc_info=True)


def _rows_to_dual_dicts(columns, rows):
    out = []
    for row in rows:
        item = {}
        for i, col in enumerate(columns):
            key = str(col or f'col{i}').strip()
            val = row[i]
            item[key] = val
            item[key.lower()] = val
        out.append(item)
    return out


def _escape_pdf_text(value):
    return str(value or '').replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _generar_pdf_fallback_basico(meta):
    """
    PDF mínimo sin dependencias externas.
    Se usa cuando WeasyPrint no está disponible en el sistema.
    """
    titulo = _escape_pdf_text("BOLETA DE PAGO - MODO COMPATIBLE")
    person = _escape_pdf_text(meta.get("person"))
    nombre = _escape_pdf_text(meta.get("nombre_trabajador") or meta.get("nombre"))
    cia = _escape_pdf_text(meta.get("cia"))
    payroll = _escape_pdf_text(meta.get("payroll_type"))
    proc = _escape_pdf_text(meta.get("process"))
    period = _escape_pdf_text(meta.get("period"))
    fecha = _escape_pdf_text(datetime.now().strftime("%d/%m/%Y %H:%M:%S"))

    lines = [
        f"BT /F1 16 Tf 50 790 Td ({titulo}) Tj ET",
        f"BT /F1 11 Tf 50 760 Td (Persona: {person}) Tj ET",
        f"BT /F1 11 Tf 50 742 Td (Nombre: {nombre}) Tj ET",
        f"BT /F1 11 Tf 50 724 Td (Compania: {cia}) Tj ET",
        f"BT /F1 11 Tf 50 706 Td (Tipo planilla: {payroll}) Tj ET",
        f"BT /F1 11 Tf 50 688 Td (Proceso: {proc}) Tj ET",
        f"BT /F1 11 Tf 50 670 Td (Periodo: {period}) Tj ET",
        f"BT /F1 9 Tf 50 640 Td (Generado: {fecha}) Tj ET",
    ]
    content_stream = ("\n".join(lines)).encode("latin-1", errors="replace")

    objects = []
    objects.append(b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    objects.append(b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    objects.append(
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
    )
    objects.append(b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    objects.append(
        f"5 0 obj << /Length {len(content_stream)} >> stream\n".encode("latin-1")
        + content_stream
        + b"\nendstream endobj\n"
    )

    pdf = io.BytesIO()
    pdf.write(b"%PDF-1.4\n")
    xref_positions = [0]
    for obj in objects:
        xref_positions.append(pdf.tell())
        pdf.write(obj)
    xref_start = pdf.tell()
    pdf.write(f"xref\n0 {len(xref_positions)}\n".encode("latin-1"))
    pdf.write(b"0000000000 65535 f \n")
    for pos in xref_positions[1:]:
        pdf.write(f"{pos:010d} 00000 n \n".encode("latin-1"))
    pdf.write(
        (
            "trailer << /Size "
            + str(len(xref_positions))
            + " /Root 1 0 R >>\nstartxref\n"
            + str(xref_start)
            + "\n%%EOF"
        ).encode("latin-1")
    )
    pdf.seek(0)
    return pdf


def _exec_sp_rows_dicts(cursor, sql, params):
    cursor.execute(sql, params)
    cols, rows = _fetch_first_nonempty_resultset(cursor)
    if not rows:
        return []
    return _rows_to_dual_dicts(cols, rows)


def get_image_base64(file_path):
    if not file_path or not os.path.exists(file_path):
        return ''
    try:
        with open(file_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except Exception:
        logging.exception('get_image_base64')
        return ''


def _bool_env(name, default=False):
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def formatear_periodo_texto(periodo_str):
    # Asumiendo formato YYYYMM... (ej: 20251212)
    meses = {
        "01": "Enero", "02": "Febrero", "03": "Marzo", "04": "Abril",
        "05": "Mayo", "06": "Junio", "07": "Julio", "08": "Agosto",
        "09": "Septiembre", "10": "Octubre", "11": "Noviembre", "12": "Diciembre",
    }
    try:
        periodo_val = str(periodo_str or "").strip()
        anio = periodo_val[:4]
        mes_num = periodo_val[4:6]
        nombre_mes = meses.get(mes_num, "Mes")
        return f"{nombre_mes} {anio}"
    except Exception:
        return str(periodo_str or "")


def enviar_correo_boleta(destinatario, nombre_empleado, periodo, sexo, pdf_io):
    """Envía boleta por Resend API con PDF adjunto."""
    if not destinatario or '@' not in str(destinatario):
        return False, "Sin correo"

    resend.api_key = os.getenv('RESEND_API_KEY')
    if not resend.api_key:
        return False, "RESEND_API_KEY no configurada"
    remitente = os.getenv('MAIL_FROM', 'onboarding@resend.dev')

    try:
        sexo_val = int(sexo)
    except Exception:
        sexo_val = 0
    trato = "Estimada" if sexo_val == 2 else "Estimado"
    periodo_legible = formatear_periodo_texto(periodo)
    pdf_base64 = base64.b64encode(pdf_io.getvalue()).decode('utf-8')

    try:
        params = {
            "from": f"Recursos Humanos <{remitente}>",
            "to": destinatario,
            "subject": f"Boleta de Pago - {periodo_legible} - {nombre_empleado}",
            "html": f"""
                <p>{trato} {nombre_empleado},</p>
                <p>Le hacemos entrega de su boleta de pago correspondiente al periodo de <b>{periodo_legible}</b>.</p>
                <p>Saludos,<br>Recursos Humanos</p>
            """,
            "attachments": [
                {
                    "content": pdf_base64,
                    "filename": f"Boleta_{periodo_legible}.pdf",
                }
            ],
        }
        resend.Emails.send(params)
        return True, "Enviado"
    except Exception as e:
        logging.error("Error en Resend: %s", str(e))
        return False, str(e)


def generar_pdf_en_memoria(params):
    cia_param = str(params.get('cia') or '').strip()
    if not cia_param and has_request_context():
        ensure_user_session()
    cia = str(cia_param or (session.get('company') if has_request_context() else '') or '').strip()
    payroll_type = str(params.get('payroll_type') or '').strip()
    processtype = str(params.get('process') or params.get('processtype') or '').strip()
    period = _normalize_pr_period(params.get('period'))
    person = str(params.get('person') or '').strip()
    if not (cia and payroll_type and processtype and period and person):
        raise ValueError('Faltan parámetros para generar boleta.')

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        _set_cursor_timeout(cursor)

        cab_rows = _exec_sp_rows_dicts(
            cursor,
            'EXEC sp_pr_generarboleta_web @cia=?, @process=?, @payrolltype=?, @period=?, @person=?',
            (cia, processtype, payroll_type, period, person),
        )
        cabecera = cab_rows[0] if cab_rows else {}

        ingresos = _exec_sp_rows_dicts(
            cursor,
            'EXEC sp_pr_detalleboletaingresos_web @cia=?, @process=?, @payrolltype=?, @period=?, @person=?',
            (cia, processtype, payroll_type, period, person),
        )
        descuentos = _exec_sp_rows_dicts(
            cursor,
            'EXEC sp_pr_detalleboletadescuentos_web @cia=?, @process=?, @payrolltype=?, @period=?, @person=?',
            (cia, processtype, payroll_type, period, person),
        )
        aportes = _exec_sp_rows_dicts(
            cursor,
            'EXEC sp_pr_detalleboletaaportes_web @cia=?, @process=?, @payrolltype=?, @period=?, @person=?',
            (cia, processtype, payroll_type, period, person),
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # Nombres de archivos configurados por compañía (tabla PR_mapping2).
    cfg = get_config_empresa(cia)
    nombre_logo = str(cfg[0]).strip() if cfg and len(cfg) > 0 and cfg[0] else 'default_logo.png'
    nombre_firma = str(cfg[1]).strip() if cfg and len(cfg) > 1 and cfg[1] else 'default_firma.png'
    ruta_logo = os.path.join(app.root_path, 'static', 'assets', nombre_logo)
    ruta_firma = os.path.join(app.root_path, 'static', 'assets', nombre_firma)
    logo_b64 = get_image_base64(ruta_logo)
    firma_b64 = get_image_base64(ruta_firma)
    if _bool_env('LOG_BOLETA_ASSETS', False):
        logging.info(
            '[boleta assets] cia=%s logo="%s" exists=%s fallback=%s | firma="%s" exists=%s fallback=%s',
            cia,
            nombre_logo,
            os.path.exists(ruta_logo),
            nombre_logo == 'default_logo.png',
            nombre_firma,
            os.path.exists(ruta_firma),
            nombre_firma == 'default_firma.png',
        )

    if WEASYPRINT_AVAILABLE:
        html_renderizado = render_template(
            'boleta_moderna.html',
            cabecera=cabecera,
            ingresos=ingresos,
            descuentos=descuentos,
            aportes=aportes,
            logo_b64=logo_b64,
            firma_b64=firma_b64,
        )
        pdf_io = io.BytesIO()
        HTML(string=html_renderizado).write_pdf(pdf_io)
        pdf_io.seek(0)
        return pdf_io

    logging.warning(
        'WeasyPrint no disponible; usando PDF fallback básico. Motivo: %s',
        _WEASYPRINT_IMPORT_ERROR,
    )
    fallback_meta = dict(cabecera or {})
    fallback_meta['person'] = person
    fallback_meta['cia'] = cia
    fallback_meta['payroll_type'] = payroll_type
    fallback_meta['process'] = processtype
    fallback_meta['period'] = period
    return _generar_pdf_fallback_basico(fallback_meta)


@login_manager.user_loader
def load_user(user_id):
    return User.get_user_by_id(user_id)


@app.route('/')
def login():
    if current_user.is_authenticated:
        ensure_user_session()
        return redirect(_url_inicio_portal())
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login_post():
    user = User.validate_user(request.form.get('username'), request.form.get('password'))
    if user:
        login_user(user)
        ensure_user_session()
        return redirect(_url_inicio_portal())
    flash('Usuario o contraseña incorrectos.', 'error')
    return redirect(url_for('login'))


@app.route('/cambiar-password', methods=['POST'])
def change_password_route():
    username = (request.form.get('username') or '').strip()
    old_password = request.form.get('old_password') or ''
    new_password = request.form.get('new_password') or ''
    confirm = request.form.get('confirm_password') or ''
    if new_password != confirm:
        flash('Las contraseñas nuevas no coinciden.', 'error')
        return redirect(url_for('login'))
    user = User.validate_user(username, old_password)
    if not user:
        flash('Usuario o contraseña anterior incorrectos.', 'error')
        return redirect(url_for('login'))
    if User.is_temp_user(user.id):
        flash('El usuario temporal no puede cambiar contraseña desde aquí.', 'warning')
        return redirect(url_for('login'))
    ok, msg = cambiar_password(user.id, old_password, new_password)
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('login'))


@app.route('/logout')
@login_required
def logout():
    session.clear()
    logout_user()
    return redirect(url_for('login'))


def _articulos_form_context(form=None, modo_edicion=False):
    """Contexto común para GET/POST del formulario de artículos."""
    return {
        'categorias': get_inventario_categorias(),
        'marcas': get_inventario_marcas(),
        'form': form or {},
        'modo_edicion': modo_edicion,
    }


def _articulos_form_desde_request():
    return {
        'iditem': (request.form.get('iditem') or '').strip(),
        'codigo': (request.form.get('codigo') or '').strip(),
        'id_categoria': request.form.get('id_categoria') or '',
        'id_marca': request.form.get('id_marca') or '',
        'descripcion': (request.form.get('descripcion') or '').strip(),
        'aplicacion': (request.form.get('aplicacion') or '').strip(),
        'codigo_bomba': (request.form.get('codigo_bomba') or '').strip(),
        'stock_inicial': (request.form.get('stock_inicial') or '0').strip(),
    }


def _item_a_form(item):
    return {
        'iditem': item.get('iditem'),
        'codigo': item.get('codigo') or '',
        'id_categoria': item.get('idcategoria') or '',
        'id_marca': item.get('idmarca') or '',
        'descripcion': item.get('descripcion') or '',
        'aplicacion': item.get('aplicacion') or '',
        'codigo_bomba': item.get('codigobomba') or '',
        'stock_inicial': item.get('stockactual') if item.get('stockactual') is not None else 0,
    }


@app.route('/configuracion/articulos', methods=['GET'])
@login_required
def articulos_page():
    """Formulario de registro de artículos (inventario)."""
    ensure_user_session()
    return render_template('articulos.html', **_articulos_form_context())


@app.route('/configuracion/articulos/editar/<int:iditem>', methods=['GET'])
@login_required
def articulos_editar_page(iditem):
    """Formulario de edición de un artículo existente."""
    ensure_user_session()
    item = get_inventario_item_por_id(iditem)
    if not item:
        flash('Artículo no encontrado.', 'error')
        return redirect(url_for('lista_articulos_page'))
    return render_template(
        'articulos.html',
        **_articulos_form_context(_item_a_form(item), modo_edicion=True),
    )


@app.route('/configuracion/articulos/guardar', methods=['POST'])
@login_required
def articulos_guardar():
    """Registra o actualiza un artículo en Inventario_Items."""
    ensure_user_session()
    form = _articulos_form_desde_request()
    iditem = form.get('iditem') or ''
    usuario_auditoria = (getattr(current_user, 'nombre', None) or getattr(current_user, 'username', '') or '').strip()

    if iditem:
        ok, msg = actualizar_inventario_item(
            iditem,
            form['id_categoria'],
            form['id_marca'],
            form['descripcion'],
            aplicacion=form['aplicacion'],
            codigo_bomba=form['codigo_bomba'],
            stock_actual=form['stock_inicial'],
            xlastuser=usuario_auditoria,
        )
        modo_edicion = True
    else:
        ok, msg = insertar_inventario_item(
            form['codigo'],
            form['id_categoria'],
            form['id_marca'],
            form['descripcion'],
            aplicacion=form['aplicacion'],
            codigo_bomba=form['codigo_bomba'],
            stock_inicial=form['stock_inicial'],
            xlastuser=usuario_auditoria,
        )
        modo_edicion = False

    if ok:
        flash(msg, 'success')
        return redirect(url_for('lista_articulos_page'))
    flash(msg, 'error')
    return render_template(
        'articulos.html',
        **_articulos_form_context(form, modo_edicion=modo_edicion),
    )


@app.route('/configuracion/articulos/eliminar', methods=['POST'])
@login_required
def articulos_eliminar():
    """Elimina un artículo por IdItem."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    iditem = body.get('iditem')
    ok, msg = eliminar_inventario_item(iditem)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 400


@app.route('/configuracion/articulos/lista')
@login_required
def lista_articulos_page():
    """Listado y búsqueda de artículos registrados."""
    ensure_user_session()
    return render_template('lista_articulos.html')


@app.route('/configuracion/articulos/listado', methods=['POST'])
@login_required
def lista_articulos_post():
    """sp_listadoarticulos_inventario @codigo, @nombre."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    codigo = str(body.get('codigo') or '').strip()
    nombre = str(body.get('nombre') or '').strip()

    headers_es = [
        'Código',
        'Descripción',
        'Categoría',
        'Aplicación',
        'Marca',
        'Stock actual',
    ]
    keys_datos = ['codigo', 'descripcion', 'categoria', 'aplicacion', 'marca', 'stockactual']

    try:
        rows = get_listado_articulos_inventario(codigo, nombre)
        resultado = []
        ids = []
        for r in rows:
            iditem = r.get('iditem')
            if iditem is None:
                for k, v in r.items():
                    if k.lower() == 'iditem':
                        iditem = v
                        break
            try:
                ids.append(int(iditem))
            except (TypeError, ValueError):
                ids.append(None)
            fila = []
            for key in keys_datos:
                val = r.get(key)
                if val is None and isinstance(r, dict):
                    for k, v in r.items():
                        if k.lower() == key.lower():
                            val = v
                            break
                if key == 'stockactual' and val is not None:
                    try:
                        fila.append(int(round(float(val))))
                    except Exception:
                        fila.append(_jsonable_value(val))
                else:
                    fila.append(_jsonable_value(val))
            resultado.append(fila)
        return jsonify({'headers': headers_es, 'data': resultado, 'ids': ids})
    except Exception as e:
        logging.exception('lista_articulos_post')
        return jsonify({'error': str(e)}), 500


def _empresas_form_context(form=None, modo_edicion=False):
    """Contexto común para GET/POST del formulario de empresas."""
    return {
        'departamentos': get_ubigeo_departamentos(),
        'form': form or {},
        'modo_edicion': modo_edicion,
    }


def _empresas_form_desde_request():
    return {
        'idempresa': (request.form.get('idempresa') or '').strip(),
        'ruc': (request.form.get('ruc') or '').strip(),
        'razon_social': (request.form.get('razon_social') or '').strip(),
        'direccion': (request.form.get('direccion') or '').strip(),
        'id_departamento': (request.form.get('id_departamento') or '').strip(),
        'id_provincia': (request.form.get('id_provincia') or '').strip(),
        'id_distrito': (request.form.get('id_distrito') or '').strip(),
        'telefono': (request.form.get('telefono') or '').strip(),
        'correo': (request.form.get('correo') or '').strip(),
        'es_cliente': request.form.get('es_cliente'),
        'es_proveedor': request.form.get('es_proveedor'),
        'estado': request.form.get('estado'),
    }


def _empresa_a_form(item):
    es_cli = item.get('escliente')
    es_prov = item.get('esproveedor')
    estado = item.get('estado')
    return {
        'idempresa': item.get('idempresa'),
        'ruc': item.get('ruc') or '',
        'razon_social': item.get('razonsocial') or '',
        'direccion': item.get('direccion') or '',
        'id_departamento': item.get('iddepartamento') or '',
        'id_provincia': item.get('idprovincia') or '',
        'id_distrito': item.get('iddistrito') or '',
        'telefono': item.get('telefono') or '',
        'correo': item.get('correo') or '',
        'es_cliente': '1' if es_cli else '',
        'es_proveedor': '1' if es_prov else '',
        'estado': '1' if estado in (None, True, 1) or str(estado) in ('1', 'True') else '',
    }


@app.route('/configuracion/empresas', methods=['GET'])
@login_required
def empresas_page():
    """Formulario de registro de clientes y proveedores."""
    ensure_user_session()
    return render_template('empresas.html', **_empresas_form_context())


@app.route('/configuracion/empresas/editar/<int:idempresa>', methods=['GET'])
@login_required
def empresas_editar_page(idempresa):
    """Formulario de edición de una empresa existente."""
    ensure_user_session()
    empresa = get_inventario_empresa_por_id(idempresa)
    if not empresa:
        flash('Empresa no encontrada.', 'error')
        return redirect(url_for('lista_empresas_page'))
    return render_template(
        'empresas.html',
        **_empresas_form_context(_empresa_a_form(empresa), modo_edicion=True),
    )


@app.route('/configuracion/empresas/guardar', methods=['POST'])
@login_required
def empresas_guardar():
    """Registra o actualiza una empresa en Inventario_Empresas."""
    ensure_user_session()
    form = _empresas_form_desde_request()
    idempresa = form.get('idempresa') or ''

    if idempresa:
        ok, msg = actualizar_inventario_empresa(
            idempresa,
            form['ruc'],
            form['razon_social'],
            direccion=form['direccion'],
            id_departamento=form['id_departamento'],
            id_provincia=form['id_provincia'],
            id_distrito=form['id_distrito'],
            telefono=form['telefono'],
            correo=form['correo'],
            es_cliente=form['es_cliente'],
            es_proveedor=form['es_proveedor'],
            estado=form['estado'],
        )
        modo_edicion = True
    else:
        ok, msg = insertar_inventario_empresa(
            form['ruc'],
            form['razon_social'],
            direccion=form['direccion'],
            id_departamento=form['id_departamento'],
            id_provincia=form['id_provincia'],
            id_distrito=form['id_distrito'],
            telefono=form['telefono'],
            correo=form['correo'],
            es_cliente=form['es_cliente'],
            es_proveedor=form['es_proveedor'],
            estado=form['estado'],
        )
        modo_edicion = False

    if ok:
        flash(msg, 'success')
        return redirect(url_for('lista_empresas_page'))
    flash(msg, 'error')
    return render_template(
        'empresas.html',
        **_empresas_form_context(form, modo_edicion=modo_edicion),
    )


@app.route('/configuracion/empresas/eliminar', methods=['POST'])
@login_required
def empresas_eliminar():
    """Elimina una empresa por IdEmpresa."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    idempresa = body.get('idempresa')
    ok, msg = eliminar_inventario_empresa(idempresa)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 400


@app.route('/configuracion/empresas/lista')
@login_required
def lista_empresas_page():
    """Listado y búsqueda de empresas registradas."""
    ensure_user_session()
    return render_template('lista_empresas.html')


@app.route('/configuracion/empresas/listado', methods=['POST'])
@login_required
def lista_empresas_post():
    """JSON para tabla de empresas."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    ruc = str(body.get('ruc') or '').strip()
    razon = str(body.get('razon_social') or body.get('nombre') or '').strip()
    tipo = str(body.get('tipo') or 'TODOS').strip().upper()
    if tipo not in ('TODOS', 'CLIENTE', 'PROVEEDOR'):
        tipo = 'TODOS'

    headers_es = [
        'RUC',
        'Razón social',
        'Teléfono',
        'Correo',
        'Cliente',
        'Proveedor',
        'Estado',
        'Ubigeo',
    ]
    keys_datos = [
        'ruc', 'razonsocial', 'telefono', 'correo',
        'escliente', 'esproveedor', 'estado', 'ubigeo',
    ]

    try:
        rows = get_listado_empresas_inventario(ruc, razon, tipo)
        resultado = []
        ids = []
        for r in rows:
            idempresa = r.get('idempresa')
            if idempresa is None:
                for k, v in r.items():
                    if k.lower() == 'idempresa':
                        idempresa = v
                        break
            try:
                ids.append(int(idempresa))
            except (TypeError, ValueError):
                ids.append(None)

            dep = _jsonable_value(r.get('nombredepartamento') or '')
            prov = _jsonable_value(r.get('nombreprovincia') or '')
            dist = _jsonable_value(r.get('nombredistrito') or '')
            partes = [p for p in (dep, prov, dist) if p]
            ubigeo_txt = ' / '.join(partes) if partes else ''

            es_cli = r.get('escliente')
            es_prov = r.get('esproveedor')
            estado_val = r.get('estado')

            fila = [
                _jsonable_value(r.get('ruc')),
                _jsonable_value(r.get('razonsocial')),
                _jsonable_value(r.get('telefono')),
                _jsonable_value(r.get('correo')),
                'Sí' if es_cli else 'No',
                'Sí' if es_prov else 'No',
                'Activo' if estado_val else 'Inactivo',
                ubigeo_txt,
            ]
            resultado.append(fila)
        return jsonify({'headers': headers_es, 'data': resultado, 'ids': ids})
    except Exception as e:
        logging.exception('lista_empresas_post')
        return jsonify({'error': str(e)}), 500


@app.route('/operaciones/compras/registro', methods=['GET'])
@login_required
def compras_registro_page():
    """Formulario de registro de compras a proveedores (ingreso a almacén)."""
    ensure_user_session()
    return render_template(
        'compras_registro.html',
        proveedores=get_proveedores_activos(),
        articulos=get_articulos_para_compra(),
        fecha_actual=_fecha_hoy_app().isoformat(),
    )


@app.route('/operaciones/compras/guardar', methods=['POST'])
@login_required
def compras_guardar():
    """Guarda cabecera y detalle de compra; actualiza stock."""
    ensure_user_session()
    id_compra = request.form.get('id_compra')
    id_proveedor = request.form.get('id_proveedor')
    fecha_compra = request.form.get('fecha_compra')
    tipo_comprobante = request.form.get('tipo_comprobante')
    nro_comprobante_ref = request.form.get('nro_comprobante_ref')
    incluye_igv = request.form.get('incluye_igv') in ('on', '1', 'true', 'True')
    estado_pago = request.form.get('estado_pago')

    detalles_raw = request.form.get('detalles_json', '[]')
    try:
        detalles = json.loads(detalles_raw) if detalles_raw else []
    except json.JSONDecodeError:
        detalles = []

    if not isinstance(detalles, list):
        detalles = []

    if id_compra:
        ok, msg = actualizar_compra(
            id_compra,
            id_proveedor,
            fecha_compra,
            tipo_comprobante,
            nro_comprobante_ref,
            incluye_igv,
            estado_pago,
            detalles,
        )
    else:
        ok, msg = insertar_compra(
            id_proveedor,
            fecha_compra,
            tipo_comprobante,
            nro_comprobante_ref,
            incluye_igv,
            estado_pago,
            detalles,
        )

    if ok:
        flash(msg, 'success')
        return redirect(url_for('lista_compras_page'))

    flash(msg, 'error')
    return render_template(
        'compras_registro.html',
        proveedores=get_proveedores_activos(),
        articulos=get_articulos_para_compra(),
        fecha_actual=fecha_compra or _fecha_hoy_app().isoformat(),
        form_preservado={
            'id_compra': id_compra,
            'id_proveedor': id_proveedor,
            'fecha_compra': fecha_compra,
            'tipo_comprobante': tipo_comprobante,
            'nro_comprobante_ref': nro_comprobante_ref,
            'incluye_igv': incluye_igv,
            'estado_pago': estado_pago,
            'detalles_json': detalles_raw,
        },
    )


@app.route('/operaciones/ventas/registro', methods=['GET'])
@login_required
def ventas_registro_page():
    """Formulario de registro de ventas a clientes (salida de almacén)."""
    ensure_user_session()
    return render_template(
        'ventas_registro.html',
        clientes=get_clientes_activos(),
        articulos=get_articulos_para_venta(),
        fecha_actual=_fecha_hoy_app().isoformat(),
    )


@app.route('/operaciones/ventas/guardar', methods=['POST'])
@login_required
def ventas_guardar():
    """Guarda cabecera y detalle de venta; descuenta stock."""
    ensure_user_session()
    id_cliente = request.form.get('id_cliente')
    fecha_venta = request.form.get('fecha_venta')
    tipo_comprobante = request.form.get('tipo_comprobante')
    nro_comprobante_ref = request.form.get('nro_comprobante_ref')
    incluye_igv = request.form.get('incluye_igv') in ('on', '1', 'true', 'True')
    estado_pago = request.form.get('estado_pago')

    detalles_raw = request.form.get('detalles_json', '[]')
    try:
        detalles = json.loads(detalles_raw) if detalles_raw else []
    except json.JSONDecodeError:
        detalles = []

    if not isinstance(detalles, list):
        detalles = []

    id_venta = request.form.get('id_venta')

    if id_venta:
        ok, msg = actualizar_venta(
            id_venta,
            id_cliente,
            fecha_venta,
            tipo_comprobante,
            nro_comprobante_ref,
            incluye_igv,
            estado_pago,
            detalles,
        )
        modo_edicion = True
    else:
        ok, msg = insertar_venta(
            id_cliente,
            fecha_venta,
            tipo_comprobante,
            nro_comprobante_ref,
            incluye_igv,
            estado_pago,
            detalles,
        )
        modo_edicion = False

    if ok:
        flash(msg, 'success')
        return redirect(url_for('lista_ventas_page'))

    flash(msg, 'error')
    return render_template(
        'ventas_registro.html',
        clientes=get_clientes_activos(),
        articulos=get_articulos_para_venta(),
        fecha_actual=fecha_venta or _fecha_hoy_app().isoformat(),
        form_preservado={
            'id_venta': id_venta,
            'id_cliente': id_cliente,
            'fecha_venta': fecha_venta,
            'tipo_comprobante': tipo_comprobante,
            'nro_comprobante_ref': nro_comprobante_ref,
            'incluye_igv': incluye_igv,
            'estado_pago': estado_pago,
            'detalles_json': detalles_raw,
            'modo_edicion': modo_edicion,
        },
    )


@app.route('/operaciones/ventas/lista', methods=['GET'])
@login_required
def lista_ventas_page():
    """Vista de listado de ventas."""
    ensure_user_session()
    return render_template('lista_ventas.html', clientes=get_clientes_activos())


@app.route('/operaciones/ventas/listado', methods=['POST'])
@login_required
def lista_ventas_post():
    """JSON de ventas usando sp_inv_lista_ventas."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    codigo = str(body.get('codigo') or '').strip()
    articulo = str(body.get('articulo') or body.get('item') or '').strip()
    try:
        cliente = int(body.get('cliente') or 0)
    except (TypeError, ValueError):
        cliente = 0

    headers_es = [
        'Cliente',
        'Fecha venta',
        'Estado venta',
        'Estado pago',
        'Código',
        'Artículo',
        'Precio unitario',
        'Cantidad',
        'Total línea',
    ]

    try:
        rows = get_lista_ventas_inventario(codigo, articulo, cliente)
        data = []
        ids = []
        for r in rows:
            id_venta = r.get('idventa')
            try:
                ids.append(int(id_venta))
            except (TypeError, ValueError):
                ids.append(None)

            fila = [
                _jsonable_value(r.get('razonsocial')),
                _jsonable_value(r.get('fechaventa')),
                _jsonable_value(r.get('estadoventa')),
                _jsonable_value(r.get('estadopago')),
                _jsonable_value(r.get('codigo')),
                _jsonable_value(r.get('descripcion')),
                _jsonable_value(r.get('preciounitario')),
                _jsonable_value(r.get('cantidad')),
                _jsonable_value(r.get('totallinea')),
            ]
            data.append(fila)
        return jsonify({'headers': headers_es, 'data': data, 'ids': ids})
    except Exception as e:
        logging.exception('lista_ventas_post')
        return jsonify({'error': str(e)}), 500


@app.route('/operaciones/ventas/anular', methods=['POST'])
@login_required
def ventas_anular():
    """Anula venta y devuelve stock al almacén."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    id_venta = body.get('id_venta')
    ok, msg = anular_venta(id_venta)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 400


@app.route('/operaciones/ventas/editar/<int:id_venta>', methods=['GET'])
@login_required
def ventas_editar_page(id_venta):
    """Formulario de ventas en modo edición."""
    ensure_user_session()
    venta = get_venta_por_id(id_venta)
    if not venta:
        flash('Venta no encontrada.', 'error')
        return redirect(url_for('lista_ventas_page'))
    if str(venta.get('estadoventa') or '').upper() == 'ANULADA':
        flash('No se puede editar una venta anulada.', 'error')
        return redirect(url_for('lista_ventas_page'))

    fecha_venta = venta.get('fechaventa')
    fecha_form = _fecha_hoy_app().isoformat()
    if hasattr(fecha_venta, 'strftime'):
        fecha_form = fecha_venta.strftime('%Y-%m-%d')
    elif isinstance(fecha_venta, str) and fecha_venta:
        fecha_form = fecha_venta[:10]

    detalles_json = json.dumps([
        {
            'id_item': d.get('iditem'),
            'cantidad': d.get('cantidad'),
            'precio_unitario': float(d.get('preciounitario') or 0),
        }
        for d in (venta.get('detalles') or [])
    ])

    return render_template(
        'ventas_registro.html',
        clientes=get_clientes_activos(),
        articulos=get_articulos_para_venta(),
        fecha_actual=fecha_form,
        form_preservado={
            'id_venta': venta.get('idventa'),
            'id_cliente': venta.get('idcliente'),
            'fecha_venta': fecha_form,
            'tipo_comprobante': venta.get('tipocomprobante') or 'FACTURA',
            'nro_comprobante_ref': venta.get('nrocomprobanteref') or '',
            'incluye_igv': bool(venta.get('incluyeigv')),
            'estado_pago': venta.get('estadopago') or 'PENDIENTE',
            'detalles_json': detalles_json,
            'modo_edicion': True,
        },
    )


@app.route('/operaciones/compras/editar/<int:id_compra>', methods=['GET'])
@login_required
def compras_editar_page(id_compra):
    """Formulario de compras en modo edición."""
    ensure_user_session()
    compra = get_compra_por_id(id_compra)
    if not compra:
        flash('Compra no encontrada.', 'error')
        return redirect(url_for('lista_compras_page'))
    if str(compra.get('estadocompra') or '').upper() == 'ANULADA':
        flash('No se puede editar una compra anulada.', 'error')
        return redirect(url_for('lista_compras_page'))

    fecha_compra = compra.get('fechacompra')
    fecha_form = _fecha_hoy_app().isoformat()
    if hasattr(fecha_compra, 'strftime'):
        fecha_form = fecha_compra.strftime('%Y-%m-%d')
    elif isinstance(fecha_compra, str) and fecha_compra:
        fecha_form = fecha_compra[:10]

    detalles_json = json.dumps([
        {
            'id_item': d.get('iditem'),
            'cantidad': d.get('cantidad'),
            'precio_unitario': float(d.get('preciounitario') or 0),
        }
        for d in (compra.get('detalles') or [])
    ])

    return render_template(
        'compras_registro.html',
        proveedores=get_proveedores_activos(),
        articulos=get_articulos_para_compra(),
        fecha_actual=fecha_form,
        form_preservado={
            'id_compra': compra.get('idcompra'),
            'id_proveedor': compra.get('idproveedor'),
            'fecha_compra': fecha_form,
            'tipo_comprobante': compra.get('tipocomprobante') or 'FACTURA',
            'nro_comprobante_ref': compra.get('nrocomprobanteref') or '',
            'incluye_igv': bool(compra.get('incluyeigv')),
            'estado_pago': compra.get('estadopago') or 'PENDIENTE',
            'detalles_json': detalles_json,
            'modo_edicion': True,
        },
    )


@app.route('/operaciones/compras/lista', methods=['GET'])
@login_required
def lista_compras_page():
    """Vista de listado de compras."""
    ensure_user_session()
    return render_template('lista_compras.html', proveedores=get_proveedores_activos())


@app.route('/operaciones/compras/listado', methods=['POST'])
@login_required
def lista_compras_post():
    """JSON de compras usando sp_inv_lista_compras."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    codigo = str(body.get('codigo') or '').strip()
    articulo = str(body.get('articulo') or body.get('item') or '').strip()
    try:
        proveedor = int(body.get('proveedor') or 0)
    except (TypeError, ValueError):
        proveedor = 0

    headers_es = [
        'Proveedor',
        'Fecha compra',
        'Estado compra',
        'Estado pago',
        'Código',
        'Artículo',
        'Precio unitario',
        'Cantidad',
        'Total línea',
    ]

    try:
        rows = get_lista_compras_inventario(codigo, articulo, proveedor)
        data = []
        ids = []
        for r in rows:
            id_compra = r.get('idcompra')
            try:
                ids.append(int(id_compra))
            except (TypeError, ValueError):
                ids.append(None)

            fila = [
                _jsonable_value(r.get('razonsocial')),
                _jsonable_value(r.get('fechacompra')),
                _jsonable_value(r.get('estadocompra')),
                _jsonable_value(r.get('estadopago')),
                _jsonable_value(r.get('codigo')),
                _jsonable_value(r.get('descripcion')),
                _jsonable_value(r.get('preciounitario')),
                _jsonable_value(r.get('cantidad')),
                _jsonable_value(r.get('totallinea')),
            ]
            data.append(fila)
        return jsonify({'headers': headers_es, 'data': data, 'ids': ids})
    except Exception as e:
        logging.exception('lista_compras_post')
        return jsonify({'error': str(e)}), 500


@app.route('/operaciones/compras/anular', methods=['POST'])
@login_required
def compras_anular():
    """Anula compra y revierte stock."""
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    id_compra = body.get('id_compra')
    ok, msg = anular_compra(id_compra)
    if ok:
        return jsonify({'ok': True, 'message': msg})
    return jsonify({'ok': False, 'error': msg}), 400


@app.route('/api/selectores/ubigeo/departamentos')
@login_required
def api_ubigeo_departamentos():
    """Departamentos para cascada de ubigeo."""
    ensure_user_session()
    rows = get_ubigeo_departamentos()
    data = [
        {'id': str(r.get('IdDepartamento', '')).strip(), 'text': str(r.get('NombreDepartamento', '')).strip()}
        for r in rows
        if r.get('IdDepartamento')
    ]
    return jsonify(data)


@app.route('/api/selectores/ubigeo/provincias')
@login_required
def api_ubigeo_provincias():
    """Provincias por departamento."""
    ensure_user_session()
    id_dep = (request.args.get('id_departamento') or '').strip()
    if not id_dep:
        return jsonify([])
    rows = get_ubigeo_provincias(id_dep)
    data = [
        {'id': str(r.get('IdProvincia', '')).strip(), 'text': str(r.get('NombreProvincia', '')).strip()}
        for r in rows
        if r.get('IdProvincia')
    ]
    return jsonify(data)


@app.route('/api/selectores/ubigeo/distritos')
@login_required
def api_ubigeo_distritos():
    """Distritos por provincia."""
    ensure_user_session()
    id_prov = (request.args.get('id_provincia') or '').strip()
    if not id_prov:
        return jsonify([])
    rows = get_ubigeo_distritos(id_prov)
    data = [
        {'id': str(r.get('IdDistrito', '')).strip(), 'text': str(r.get('NombreDistrito', '')).strip()}
        for r in rows
        if r.get('IdDistrito')
    ]
    return jsonify(data)


@app.route('/dashboard')
@login_required
def dashboard():
    """Panel inicial con acceso a los módulos de inventario."""
    ensure_user_session()
    nombre = ''
    if current_user.is_authenticated:
        nombre = str(getattr(current_user, 'nombre', None) or current_user.username or '').strip()
        if not nombre:
            nombre = str(session.get('simple_lock_person_name') or '').strip()
    return render_template('dashboard.html', nombre_bienvenida=nombre)


@app.route('/mis-documentos/<tipodoc>')
@login_required
def mis_documentos_simple_page(tipodoc):
    """Vista reducida de documentos del personal (perfil SIMPLE, uso móvil)."""
    ensure_user_session()
    if not session.get('simple_profile'):
        return redirect(url_for('reporte_documentos_personal_page'))
    tipo = _tipodoc_web_por_codigo(tipodoc)
    if not tipo:
        flash('Tipo de documento no válido.', 'warning')
        return redirect(url_for('dashboard'))
    lock_cia = _documentos_effective_company_lock()
    ctx = _documentos_personal_template_context()
    ctx.update({
        'tipo_documento_codigo': tipo['codigo'],
        'tipo_documento_nombre': tipo['nombre'],
        'tipo_documento_icono': tipo['icono'],
        'simple_cia_codigo': lock_cia or str(session.get('company') or '').strip(),
    })
    return render_template('reporte_documentos_personal_simple.html', **ctx)


def _ruta_carga_documentos_efectiva(user_id):
    """Identificador de carpeta de Google Drive (guardado en SY_User.RutaDocumentos)."""
    return get_ruta_documentos_usuario(user_id)


def _carpeta_sustento_vacaciones_desde_env():
    """
    ID de carpeta en unidad compartida (Shared Drive) para sustentos de vacaciones.

    Solo GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES (Render / .env).
    No usa SY_User.RutaDocumentos (reservado a boletas / Cargar documentos).
    """
    raw_env = str(os.getenv('GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES') or '').strip()
    folder_id = _normalizar_folder_id_drive(raw_env)
    return {
        'folder_id': folder_id,
        'source': 'env:GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES',
        'raw': raw_env,
        'normalized': folder_id,
    }


def _procesar_carga_desde_carpeta_local_respaldo(ruta_servidor):
    """
    Respaldo de lógica antigua (escaneo de carpeta local en servidor).
    Conservada para contingencia, no usada en el flujo actual.
    """
    archivos = [f for f in os.listdir(ruta_servidor) if f.lower().endswith('.pdf')]
    nuevos = 0
    omitidos_formato = 0
    for nombre_archivo in archivos:
        base = nombre_archivo[:-4] if nombre_archivo.lower().endswith('.pdf') else nombre_archivo
        base = base.strip()
        if base.count('_') < 3:
            omitidos_formato += 1
            continue
        partes = base.split('_')
        if len(partes) < 4:
            omitidos_formato += 1
            continue
        datos = {
            'tipo': str(partes[0]).strip(),
            'periodo': str(partes[1]).strip(),
            'dni': str(partes[2]).strip(),
            'nombre': ' '.join(str(p).strip() for p in partes[3:] if str(p).strip()).strip(),
            'archivo_original': nombre_archivo,
        }
        if insertar_documento_minero(datos):
            nuevos += 1
    return {
        'encontrados': len(archivos),
        'nuevos': nuevos,
        'omitidos_formato': omitidos_formato,
    }


def _resolver_credenciales_drive():
    """
    Ruta del JSON de credenciales de Service Account (solo archivo en disco).
    Para Render sin archivo en el repo, use GOOGLE_DRIVE_CREDENTIALS_JSON (ver _credentials_drive_service_account).

    Prioridad:
      1) GOOGLE_DRIVE_CREDENTIALS_FILE
      2) SERVICE_ACCOUNT_FILE
      3) google_keys.json en la raíz del proyecto (si es service_account)
      4) primer *.json del proyecto con "type": "service_account"
    """
    from pathlib import Path

    root = Path(app.root_path)

    for env_key in ('GOOGLE_DRIVE_CREDENTIALS_FILE', 'SERVICE_ACCOUNT_FILE'):
        cfg = str(os.getenv(env_key) or '').strip()
        if not cfg:
            continue
        p = Path(cfg)
        if not p.is_absolute():
            p = root / p
        if p.is_file():
            try:
                with p.open('r', encoding='utf-8') as f:
                    data = json.load(f)
                if str(data.get('type') or '').strip() != 'service_account':
                    logging.warning(
                        'Drive: el JSON en %s no tiene type=service_account (clave type=%r).',
                        p,
                        data.get('type'),
                    )
                    continue
            except Exception as e:
                logging.warning('Drive: no se pudo leer el JSON de credenciales %s: %s', p, e)
                continue
            return str(p)

    for fname in ('google_keys.json',):
        p = root / fname
        if not p.is_file():
            continue
        try:
            with p.open('r', encoding='utf-8') as f:
                data = json.load(f)
            if str(data.get('type') or '').strip() == 'service_account':
                return str(p)
        except Exception as e:
            logging.warning('Drive: error al leer %s: %s', p, e)
            continue

    for p in root.glob('*.json'):
        try:
            with p.open('r', encoding='utf-8') as f:
                data = json.load(f)
            if str(data.get('type') or '').strip() == 'service_account':
                return str(p)
        except Exception:
            continue
    return None


def _runtime_error_google_api(exc):
    """Convierte fallos de red/API de Google en RuntimeError con texto útil para el usuario."""
    msg = str(exc)
    if isinstance(exc, TimeoutError) or '10060' in msg or 'timed out' in msg.lower() or 'timeout' in msg.lower():
        return RuntimeError(
            'No se pudo conectar con Google (timeout). Revise: (1) firewall de Windows o de terceros — permita '
            'Python y HTTPS a googleapis.com y oauth2.googleapis.com; (2) VPN o red corporativa; (3) Wi‑Fi o '
            'enlace inestable; (4) proxy (HTTPS_PROXY si aplica). Opcional: variable GOOGLE_HTTP_TIMEOUT (segundos, '
            'p. ej. 180) para alargar el tiempo de espera. '
            f'Detalle: {msg}'
        )
    return RuntimeError(
        'Error al usar la API de Google Drive. Revise el JSON de service account, que la carpeta esté '
        f'compartida con la cuenta del JSON y la red. Detalle: {msg}'
    )


def _drive_es_timeout_o_red(exc, depth=0):
    """True si la excepción (o su __cause__) indica timeout o error típico de conexión."""
    if depth > 8 or exc is None:
        return False
    if isinstance(exc, TimeoutError):
        return True
    msg = str(exc)
    if '10060' in msg or 'timed out' in msg.lower() or 'timeout' in msg.lower():
        return True
    return _drive_es_timeout_o_red(getattr(exc, '__cause__', None), depth + 1)


def _codigo_error_drive_para_soporte(exc):
    """Código corto para que el usuario lo comunique a soporte (sin detalle técnico)."""
    return 'DRIVE_TIMEOUT' if _drive_es_timeout_o_red(exc) else 'DRIVE_UNAVAILABLE'


def _es_timeout_worker_sync(exc, depth=0):
    """
    True si el proceso fue abortado por tiempo (p. ej. Gunicorn WORKER TIMEOUT).
    Suele verse como SystemError de pyodbc al cortar cursor.execute a mitad de lote.
    """
    if depth > 8 or exc is None:
        return False
    if isinstance(exc, SystemError):
        return True
    msg = str(exc).lower()
    if 'worker timeout' in msg or 'handle_abort' in msg:
        return True
    return _es_timeout_worker_sync(getattr(exc, '__cause__', None), depth + 1)


def _es_datos_truncados_sql_sync(exc, depth=0):
    """8152 / 22001: algún campo supera el tamaño de columna en DocumentosBoletas."""
    from database import _es_error_truncado_sql

    if depth > 8 or exc is None:
        return False
    if _es_error_truncado_sql(exc):
        return True
    return _es_datos_truncados_sql_sync(getattr(exc, '__cause__', None), depth + 1)


def _codigo_error_sync_carga(exc):
    if _es_timeout_worker_sync(exc):
        return 'SYNC_TIMEOUT'
    if _es_datos_truncados_sql_sync(exc):
        return 'SYNC_DATA_TRUNCATED'
    return _codigo_error_drive_para_soporte(exc)


def _mensaje_error_sync_carga(exc):
    if _es_timeout_worker_sync(exc):
        return (
            'La sincronización tardó más de lo permitido en el servidor y se interrumpió. '
            'Los documentos ya procesados quedaron guardados: pulse de nuevo «Sincronizar desde Google Drive» '
            'para continuar con el resto. Si el fallo se repite en el mismo punto, contacte a soporte o al área de sistemas.'
        )
    if _es_datos_truncados_sql_sync(exc):
        return (
            'Un archivo tiene un nombre demasiado largo para la base de datos y detuvo la sincronización. '
            'Los documentos ya guardados permanecen. Acorte en Drive el nombre del PDF '
            '(sobre todo la parte del empleado tras el DNI) y vuelva a sincronizar.'
        )
    return _mensaje_error_drive_para_usuario(exc)


def _mensaje_error_drive_para_usuario(exc):
    """
    Texto para quien usa la web: no menciona Python, firewalls ni variables de entorno.
    La conexión real a Google la hace el servidor; el cliente solo ve este mensaje.
    """
    if _drive_es_timeout_o_red(exc):
        return (
            'En este momento el almacenamiento de documentos no respondió a tiempo. '
            'Intente de nuevo en unos minutos. Si el problema continúa, contacte a soporte o al área de sistemas.'
        )
    return (
        'No pudimos obtener el documento desde el almacenamiento en este momento. '
        'Si el problema continúa, contacte a soporte o al área de sistemas.'
    )


def _mensaje_error_descarga_drive(exc):
    """Alias hacia mensaje de usuario final (descarga desde reporte / flash)."""
    return _mensaje_error_drive_para_usuario(exc)


def _drive_http_timeout_seconds():
    """Timeout de socket para Drive (httplib2). Env GOOGLE_HTTP_TIMEOUT, por defecto 180 s, mínimo 30."""
    try:
        return max(30, int(os.getenv('GOOGLE_HTTP_TIMEOUT', '180')))
    except (TypeError, ValueError):
        return 180


def _drive_refresh_credentials_if_needed(creds):
    """
    Refresca el access token con requests si hace falta.
    En algunos equipos Windows es más estable que el primer refresh vía httplib2.
    """
    if not creds:
        return
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
    except Exception:
        return
    try:
        if getattr(creds, 'expired', False) or not getattr(creds, 'valid', True):
            creds.refresh(GoogleAuthRequest())
    except Exception:
        logging.debug('Drive: refresh previo con requests omitido', exc_info=True)


def _descarga_personal_es_fetch():
    """True si el cliente pide errores en JSON (descarga desde el reporte con fetch)."""
    return request.headers.get('X-Fetch-Descarga') == '1'


def _meta_carpeta_drive(folder_id):
    """Metadatos de carpeta Drive (acceso service account). None si no se puede leer."""
    try:
        service = _build_drive_service()
        return service.files().get(
            fileId=str(folder_id or '').strip(),
            fields='id,name,mimeType,driveId',
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        logging.warning('Drive: no se pudo leer carpeta %s: %s', folder_id, e)
        return None


def _listar_archivos_pdf_drive(folder_id, incluir_subcarpetas=True):
    """
    Lista PDF en una carpeta de Google Drive (y subcarpetas si incluir_subcarpetas).
    Retorna [{'name', 'id'}, ...]. Usa supportsAllDrives para carpetas compartidas / unidades compartidas.
    """
    fid_raiz = str(folder_id or '').strip()
    if not fid_raiz:
        return []
    mime_pdf = 'application/pdf'
    mime_folder = 'application/vnd.google-apps.folder'
    try:
        service = _build_drive_service()
        archivos = []
        pendientes = [fid_raiz]
        visitadas = set()

        while pendientes:
            fid = pendientes.pop(0)
            if fid in visitadas:
                continue
            visitadas.add(fid)
            page_token = None
            while True:
                q = f"'{fid}' in parents and trashed=false"
                resp = service.files().list(
                    q=q,
                    corpora='allDrives',
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    fields='nextPageToken, files(id, name, mimeType)',
                    pageSize=1000,
                    pageToken=page_token,
                ).execute()
                for f in resp.get('files') or []:
                    mime = str(f.get('mimeType') or '')
                    if mime == mime_pdf:
                        archivos.append({
                            'id': f.get('id'),
                            'name': f.get('name'),
                        })
                    elif incluir_subcarpetas and mime == mime_folder:
                        sub_id = str(f.get('id') or '').strip()
                        if sub_id:
                            pendientes.append(sub_id)
                page_token = resp.get('nextPageToken')
                if not page_token:
                    break

        logging.info(
            'Drive list: folder_id=%s subcarpetas=%s carpetas_visitadas=%s pdfs=%s',
            fid_raiz,
            incluir_subcarpetas,
            len(visitadas),
            len(archivos),
        )
        print(
            f'[Drive sync] folder_id={fid_raiz!r} pdfs_encontrados={len(archivos)} '
            f'subcarpetas={incluir_subcarpetas}',
            flush=True,
        )
        return archivos
    except RuntimeError:
        raise
    except Exception as e:
        logging.exception('_listar_archivos_pdf_drive folder_id=%s', fid_raiz)
        raise _runtime_error_google_api(e) from e


def _mensaje_sync_sin_pdfs_drive(folder_id, stats, rename_stats):
    """Texto de ayuda cuando la sincronización no encontró o procesó PDF."""
    meta = _meta_carpeta_drive(folder_id)
    nombre_carpeta = (meta or {}).get('name') if meta else None
    partes = [
        f'No se sincronizó ningún PDF. Carpeta configurada (ID): {folder_id}.',
    ]
    if nombre_carpeta:
        partes.append(f'Nombre en Drive: {nombre_carpeta}.')
    if meta and meta.get('driveId'):
        partes.append('(Unidad compartida detectada.)')
    partes.append(
        'Revise: (1) compartir la carpeta BOLETA (y subcarpetas como 2026) con la service account '
        'portal-minero-drive-access@… como Editor; (2) usar el ID de la carpeta donde están los PDF '
        '(o una carpeta padre: ahora también se buscan PDF en subcarpetas); '
        '(3) credenciales GOOGLE_DRIVE_CREDENTIALS_JSON en Render.'
    )
    if stats.get('omitidos_formato'):
        partes.append(
            f"Archivos con formato no válido: {stats.get('omitidos_formato')}."
        )
    if stats.get('omitidos_largo'):
        partes.append(
            f"Archivos con nombre demasiado largo para la base de datos: {stats.get('omitidos_largo')} "
            '(acorte el nombre del empleado en el PDF).'
        )
    if rename_stats.get('detectados'):
        partes.append(
            f"FIN_DE_MES detectados: {rename_stats.get('detectados')}, "
            f"renombrados: {rename_stats.get('renombrados')}."
        )
    return ' '.join(partes)


def _normalizar_nombre_boleta_fin_de_mes(nombre_archivo):
    """
    Convierte periodo_FIN_DE_MES_dni_nombre.pdf → BOL_periodo_dni_nombre.pdf.
    Retorna None si el nombre no requiere cambio.
    """
    nombre = str(nombre_archivo or '').strip()
    if not nombre or 'FIN_DE_MES' not in nombre.upper():
        return None
    lower = nombre.lower()
    ext = '.pdf' if lower.endswith('.pdf') else ''
    base = nombre[:-4] if ext else nombre
    if 'FIN_DE_MES' not in base.upper():
        return None
    nuevo_base = re.sub(r'FIN_DE_MES', 'BOL', base, flags=re.IGNORECASE)
    m = re.match(r'^(\d+)_BOL_(.+)$', nuevo_base, re.IGNORECASE)
    if m:
        nuevo_base = f'BOL_{m.group(1)}_{m.group(2)}'
    nuevo = f'{nuevo_base}{ext}'
    return nuevo if nuevo != nombre else None


def _renombrar_boletas_fin_de_mes_en_drive(archivos):
    """
    Renombra en Drive los PDF con FIN_DE_MES al formato BOL_Periodo_DNI_Nombre
    y actualiza el campo name en la lista antes de sincronizar metadata.
    """
    stats = {'detectados': 0, 'renombrados': 0, 'fallidos': 0}
    pendientes = []
    for item in archivos or []:
        nombre = str((item or {}).get('name') or '').strip()
        file_id = str((item or {}).get('id') or '').strip()
        nuevo = _normalizar_nombre_boleta_fin_de_mes(nombre)
        if nuevo and file_id:
            stats['detectados'] += 1
            pendientes.append((item, file_id, nombre, nuevo))

    if not pendientes:
        return stats

    service = None
    HttpError = None
    try:
        from googleapiclient.errors import HttpError as _HttpError

        HttpError = _HttpError
        service = _build_drive_service()
    except Exception as e:
        logging.warning(
            'Rename FIN_DE_MES: no se pudo usar API Drive (%s); se sincroniza con nombre normalizado.',
            e,
        )

    for item, file_id, nombre, nuevo in pendientes:
        if service:
            try:
                service.files().update(
                    fileId=file_id,
                    body={'name': nuevo},
                    supportsAllDrives=True,
                ).execute()
                item['name'] = nuevo
                stats['renombrados'] += 1
                logging.info('Boleta Drive renombrada: %r -> %r', nombre, nuevo)
                continue
            except Exception as e:
                stats['fallidos'] += 1
                reason = _drive_http_error_reason(e) if HttpError and isinstance(e, HttpError) else str(e)
                logging.warning('No se pudo renombrar en Drive %r: %s', nombre, reason)
        item['name'] = nuevo
        if not service:
            stats['renombrados'] += 1
    return stats


def _credentials_drive_service_account(scopes=None, subject=None):
    """
    Credenciales OAuth de la service account para la API de Drive.

    subject: correo de usuario Workspace para delegación (domain-wide delegation).
    Solo aplica en subida de sustentos si GOOGLE_DRIVE_SUSTENTO_IMPERSONATE_USER está definido.

    Orden (recomendado en Render: JSON como secreto, sin subir archivo al repo):
      1) GOOGLE_DRIVE_CREDENTIALS_JSON — contenido completo del JSON (una sola línea o compacto).
      2) GOOGLE_SERVICE_ACCOUNT_JSON — alias del anterior.
      3) Archivo en disco vía _resolver_credenciales_drive() (GOOGLE_DRIVE_CREDENTIALS_FILE, etc.).
    """
    try:
        from google.oauth2 import service_account
    except Exception as e:
        raise RuntimeError(
            'Faltan dependencias de Google Drive. Instale google-api-python-client y google-auth.'
        ) from e

    scopes = scopes or ['https://www.googleapis.com/auth/drive.readonly']

    for env_name in ('GOOGLE_DRIVE_CREDENTIALS_JSON', 'GOOGLE_SERVICE_ACCOUNT_JSON'):
        raw = str(os.getenv(env_name) or '').strip()
        if not raw:
            continue
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f'La variable de entorno {env_name} no contiene JSON válido. En Render pegue el JSON completo '
                f'como secreto (sin comillas externas). Detalle: {e}'
            ) from e
        if str(info.get('type') or '').strip() != 'service_account':
            raise RuntimeError(
                f'La variable {env_name} debe ser un JSON de Google con "type": "service_account".'
            )
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        if subject:
            creds = creds.with_subject(str(subject).strip())
        return creds

    cred_path = _resolver_credenciales_drive()
    if not cred_path:
        raise RuntimeError(
            'No hay credenciales de Google Drive. En Render: cree un secreto GOOGLE_DRIVE_CREDENTIALS_JSON con el '
            'contenido del archivo JSON de la service account. En local puede usar GOOGLE_DRIVE_CREDENTIALS_FILE o '
            'SERVICE_ACCOUNT_FILE con la ruta al JSON.'
        )
    creds = service_account.Credentials.from_service_account_file(cred_path, scopes=scopes)
    if subject:
        creds = creds.with_subject(str(subject).strip())
    return creds


def _usuario_impersonacion_sustento_drive():
    """Correo Workspace para subir sustentos en nombre del usuario (evita storageQuotaExceeded de la SA)."""
    for key in ('GOOGLE_DRIVE_SUSTENTO_IMPERSONATE_USER', 'GOOGLE_DRIVE_DELEGATED_USER'):
        val = str(os.getenv(key) or '').strip()
        if val:
            return val
    return ''


def _build_drive_service(scopes=None, subject=None):
    """
    Cliente Drive (service account). Refresca token con requests si aplica y usa AuthorizedHttp con timeout
    largo para reducir timeouts intermitentes (WinError 10060) en redes lentas o firewalls lentos.
    """
    try:
        from googleapiclient.discovery import build
    except Exception as e:
        raise RuntimeError(
            'Faltan dependencias de Google Drive. Instale google-api-python-client y google-auth.'
        ) from e

    creds = _credentials_drive_service_account(scopes, subject=subject)
    _drive_refresh_credentials_if_needed(creds)
    timeout_s = _drive_http_timeout_seconds()
    try:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp

        http = httplib2.Http(timeout=timeout_s)
        authed = AuthorizedHttp(creds, http=http)
        return build('drive', 'v3', http=authed, cache_discovery=False)
    except Exception as e:
        logging.warning(
            'Drive: no se pudo usar AuthorizedHttp (timeout=%ss), cliente por defecto: %s',
            timeout_s,
            e,
        )
        return build('drive', 'v3', credentials=creds, cache_discovery=False)


def _descargar_archivo_drive(file_id):
    """
    Descarga archivo de Drive desde backend (service account), evitando 403 en navegador del usuario.
    Retorna (BytesIO, nombre_archivo, mime_type).
    """
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except Exception as e:
        raise RuntimeError(
            "Falta google-api-python-client para descarga desde Drive."
        ) from e

    service = _build_drive_service()
    meta = service.files().get(
        fileId=file_id,
        fields='name,mimeType',
        supportsAllDrives=True,
    ).execute()
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    nombre = str(meta.get('name') or f'{file_id}.pdf')
    mime = str(meta.get('mimeType') or 'application/pdf')
    return fh, nombre, mime


def _nombre_archivo_sustento_vacaciones(dni, periodo):
    """Nombre en Drive: Sustento_Vacaciones_[DNI]_[Periodo].pdf"""
    dni_s = re.sub(r'[^\w\-]+', '_', str(dni or '').strip())[:40] or 'SIN_DNI'
    per_s = re.sub(r'[^\w\-]+', '_', str(periodo or '').strip())[:20] or 'SIN_PERIODO'
    return f'Sustento_Vacaciones_{dni_s}_{per_s}.pdf'


def _drive_http_error_reason(exc):
    """Extrae motivo legible de un HttpError de la API de Drive."""
    try:
        import json
        content = getattr(getattr(exc, 'resp', None), 'content', b'') or b''
        if isinstance(content, bytes):
            content = content.decode('utf-8', errors='replace')
        if content:
            data = json.loads(content)
            err = data.get('error') or {}
            return str(err.get('message') or err.get('status') or content)[:500]
    except Exception:
        pass
    return str(exc)


def _subir_pdf_sustento_drive(folder_id, nombre_archivo, archivo_stream):
    """Sube un PDF a la carpeta de unidad compartida (GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES). Retorna file_id."""
    try:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseUpload
    except Exception as e:
        raise RuntimeError(
            'Falta google-api-python-client para subir archivos a Drive.'
        ) from e

    parent_id = str(folder_id or '').strip()
    nombre = str(nombre_archivo or 'Sustento_Vacaciones.pdf').strip()
    logging.info(
        'Drive sustento unidad compartida: env=GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES '
        'parents=%r name=%r supportsAllDrives=True',
        parent_id,
        nombre,
    )
    print(
        '[Drive sustento] GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES '
        f'parents={parent_id!r} file_name={nombre!r} supportsAllDrives=True',
        flush=True,
    )

    drive_service = _build_drive_service(['https://www.googleapis.com/auth/drive'])

    try:
        carpeta = drive_service.files().get(
            fileId=parent_id,
            fields='id,name,driveId,mimeType,capabilities',
            supportsAllDrives=True,
        ).execute()
        logging.info(
            'Drive sustento: carpeta OK id=%s driveId=%s name=%r canAddChildren=%s',
            carpeta.get('id'),
            carpeta.get('driveId'),
            carpeta.get('name'),
            (carpeta.get('capabilities') or {}).get('canAddChildren'),
        )
        drive_id = str(carpeta.get('driveId') or '').strip()
        print(
            f"[Drive sustento] carpeta verificada id={carpeta.get('id')} "
            f"driveId={drive_id!r} es_unidad_compartida={bool(drive_id)}",
            flush=True,
        )
        if not drive_id:
            raise RuntimeError(
                'La carpeta de GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES no está en una '
                'Unidad compartida (Shared Drive): driveId vacío. Las service accounts no tienen '
                'cuota en "Mi unidad" aunque la carpeta esté compartida como Editor. '
                'Cree CONSTANCIASVAC dentro de Unidades compartidas (no en Mi unidad), copie el '
                'ID nuevo de esa carpeta y actualice la variable en Render. '
                f'ID actual configurado: {parent_id}'
            )
        caps = carpeta.get('capabilities') or {}
        if caps.get('canAddChildren') is False:
            raise RuntimeError(
                'La service account no puede agregar archivos en esa carpeta de la unidad compartida. '
                'Verifique que sea administradora de contenido en la unidad compartida.'
            )
    except HttpError as e:
        status = getattr(getattr(e, 'resp', None), 'status', None)
        reason = _drive_http_error_reason(e)
        logging.exception(
            'Drive sustento: get carpeta falló parents=%r supportsAllDrives=True reason=%s',
            parent_id,
            reason,
        )
        if status == 404:
            raise RuntimeError(
                'Carpeta no encontrada (404). Verifique GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES '
                f'(ID de unidad compartida: {parent_id}).'
            ) from e
        raise RuntimeError(
            f'No se puede acceder a la carpeta de sustentos (HTTP {status}): {reason}'
        ) from e

    media = MediaIoBaseUpload(archivo_stream, mimetype='application/pdf', resumable=False)
    file_metadata = {
        'name': nombre,
        'parents': [parent_id],
    }
    try:
        created = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        status = getattr(getattr(e, 'resp', None), 'status', None)
        reason = _drive_http_error_reason(e)
        logging.exception(
            'Drive sustento: create falló parents=%r name=%r supportsAllDrives=True reason=%s',
            parent_id,
            nombre,
            reason,
        )
        print(
            f'[Drive sustento] ERROR create status={status} parents={parent_id!r} '
            f'supportsAllDrives=True reason={reason!r}',
            flush=True,
        )
        if status == 404:
            raise RuntimeError(
                'Carpeta no encontrada al subir (404). Actualice GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES '
                'con el ID de la carpeta dentro de la unidad compartida.'
            ) from e
        if status == 403 and (
            'storageQuotaExceeded' in reason
            or 'storage quota' in reason.lower()
            or 'do not have storage quota' in reason.lower()
        ):
            raise RuntimeError(
                'Google Drive: storageQuotaExceeded. La service account no puede crear archivos '
                'en "Mi unidad" (sin cuota propia). Aunque use Workspace, el ID en '
                'GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES debe ser una carpeta DENTRO de una '
                'Unidad compartida (revise en logs driveId=...; si está vacío, el ID es de Mi unidad). '
                'Si movió CONSTANCIASVAC a una unidad compartida, copie el ID nuevo de esa carpeta '
                '(el ID antiguo 1CAnJBa_... de Mi unidad ya no sirve). '
                f'Detalle API: {reason}'
            ) from e
        if status == 403:
            raise RuntimeError(
                f'Google Drive rechazó la subida (403): {reason}'
            ) from e
        raise RuntimeError(
            f'Error al subir a Google Drive (HTTP {status}): {reason}'
        ) from e

    file_id = str(created.get('id') or '').strip()
    if not file_id:
        raise RuntimeError('Drive no devolvió el identificador del archivo subido.')

    logging.info(
        'Drive sustento: archivo creado en unidad compartida file_id=%s parents=%s',
        file_id,
        parent_id,
    )
    print(f'[Drive sustento] OK file_id={file_id!r} parents={parent_id!r}', flush=True)
    return file_id


def _normalizar_folder_id_drive(valor):
    """
    Acepta ID puro o URL de carpeta de Google Drive y retorna el folder_id.
    Retorna None si no parece un ID/URL válido.
    """
    raw = str(valor or '').strip()
    if not raw:
        return None

    # Si pegaron URL de Drive, extraer el ID
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)

    # Aceptar ID directo (alfanumérico + _-)
    if re.match(r"^[A-Za-z0-9_-]{10,}$", raw):
        return raw

    return None


@app.route('/configuracion-usuario', methods=['GET', 'POST'])
@login_required
def configuracion_usuario():
    if request.method == 'POST':
        ruta = request.form.get('ruta_documentos', '')
        ok, msg = update_ruta_documentos_usuario(current_user.id, ruta)
        flash(msg, 'success' if ok else 'error')
        return redirect(url_for('configuracion_usuario'))

    ruta_actual = get_ruta_documentos_usuario(current_user.id) or ''
    return render_template(
        'configuracion_usuario.html',
        ruta_actual=ruta_actual,
    )


@app.route('/carga-documentos')
@login_required
def carga_documentos():
    ruta_efectiva = _ruta_carga_documentos_efectiva(current_user.id)
    return render_template(
        'carga_documentos.html',
        ruta_servidor=ruta_efectiva,
    )


@app.route('/procesar-carga-servidor', methods=['POST'])
@login_required
def procesar_carga_servidor():
    folder_cfg = _ruta_carga_documentos_efectiva(current_user.id)
    folder_id = _normalizar_folder_id_drive(folder_cfg)
    if not folder_id:
        flash(
            'La carpeta de Google Drive no es válida. Guarde el ID de carpeta o pegue la URL de Drive en Configuración por usuario.',
            'error',
        )
        return redirect(url_for('carga_documentos'))
    try:
        archivos_drive = _listar_archivos_pdf_drive(folder_id)
        rename_stats = _renombrar_boletas_fin_de_mes_en_drive(archivos_drive)
        stats = sincronizar_metadata_drive(archivos_drive)
        ok_sp, msg_sp = ejecutar_sp_updatecompany_documentos_boletas()

        resumen = (
            f'Sincronización finalizada. PDF en Drive: {len(archivos_drive)}. '
            f'Renombrados FIN_DE_MES→BOL: {rename_stats.get("renombrados", 0)} '
            f'(detectados: {rename_stats.get("detectados", 0)}, '
            f'fallidos en Drive: {rename_stats.get("fallidos", 0)}). '
            f'Procesados: {stats.get("procesados", 0)}. '
            f'Sincronizados (insert/update): {stats.get("ok", 0)}. '
            f'Omitidos por formato: {stats.get("omitidos_formato", 0)}. '
            f'Omitidos (nombre muy largo): {stats.get("omitidos_largo", 0)}. '
            f'Sin ID de Drive: {stats.get("sin_id", 0)}.'
        )
        flash(resumen, 'success' if stats.get('ok') else 'warning')
        if not stats.get('ok'):
            flash(
                _mensaje_sync_sin_pdfs_drive(folder_id, stats, rename_stats),
                'warning',
            )
        if ok_sp:
            flash(msg_sp, 'success')
        else:
            flash(msg_sp, 'error')
    except Exception as e:
        logging.exception('procesar_carga_servidor')
        flash(_mensaje_error_drive_para_usuario(e), 'error')

    return redirect(url_for('carga_documentos'))


CARGA_DOCUMENTOS_BATCH_SYNC = 50


@app.route('/api/carga-documentos/sincronizar', methods=['POST'])
@login_required
def api_carga_documentos_sincronizar():
    """
    Sincroniza PDF de Drive contra DocumentosBoletas por lotes y emite NDJSON
    (start → progress* → done | error) para barra de progreso en el cliente.
    """
    folder_cfg = _ruta_carga_documentos_efectiva(current_user.id)
    folder_id = _normalizar_folder_id_drive(folder_cfg)
    if not folder_id:
        return jsonify(
            error=(
                'La carpeta de Google Drive no es válida. Guarde el ID de carpeta o la URL '
                'en Configuración por usuario.'
            )
        ), 400

    batch = max(1, int(CARGA_DOCUMENTOS_BATCH_SYNC))

    def generate():
        conn = None
        cursor = None
        try:
            archivos = _listar_archivos_pdf_drive(folder_id)
            rename_stats = _renombrar_boletas_fin_de_mes_en_drive(archivos)
            total = len(archivos)
            yield (
                json.dumps(
                    {'type': 'start', 'total': total, 'rename': rename_stats},
                    ensure_ascii=False,
                )
                + '\n'
            )

            cum = {
                'procesados': 0,
                'omitidos_formato': 0,
                'omitidos_largo': 0,
                'sin_id': 0,
                'ok': 0,
            }
            conn = get_db_connection()
            cursor = conn.cursor()

            for i in range(0, total, batch):
                chunk = archivos[i : i + batch]
                sincronizar_metadata_drive_lote(cursor, chunk, cum)
                conn.commit()
                current = min(i + len(chunk), total)
                yield (
                    json.dumps(
                        {
                            'type': 'progress',
                            'current': current,
                            'total': total,
                            'stats': dict(cum),
                        },
                        ensure_ascii=False,
                    )
                    + '\n'
                )

            if cursor:
                cursor.close()
                cursor = None
            if conn:
                conn.close()
                conn = None

            ok_sp, msg_sp = ejecutar_sp_updatecompany_documentos_boletas()
            done_payload = {
                'type': 'done',
                'total': total,
                'stats': dict(cum),
                'rename': rename_stats,
                'folder_id': folder_id,
                'sp_ok': ok_sp,
                'sp_msg': msg_sp,
            }
            if not cum.get('ok'):
                done_payload['hint'] = _mensaje_sync_sin_pdfs_drive(
                    folder_id, cum, rename_stats
                )
            yield json.dumps(done_payload, ensure_ascii=False) + '\n'
        except Exception as e:
            logging.exception('api_carga_documentos_sincronizar')
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            yield (
                json.dumps(
                    {
                        'type': 'error',
                        'message': _mensaje_error_sync_carga(e),
                        'code': _codigo_error_sync_carga(e),
                    },
                    ensure_ascii=False,
                )
                + '\n'
            )
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype='application/x-ndjson',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/reporte-liquidaciones')
@login_required
def reporte_liquidaciones():
    # La página carga vacía; los filtros se llenan por JS vía APIs.
    return render_template('reporte_liquidaciones.html')


@app.route('/reporte-planilla-vertical')
@login_required
def reporte_planilla_vertical_page():
    return render_template('reporte_planilla_vertical.html')


@app.route('/reporte-vacaciones-detalle')
@login_required
def reporte_vacaciones_detalle_page():
    return render_template('reporte_vacaciones_detalle.html', **_reporte_filtros_perfil_template_context())


@app.route('/reporte-aprobar-vacaciones')
@login_required
def reporte_aprobar_vacaciones_page():
    ensure_user_session()
    if not _usuario_perfil_general_o_minero():
        flash('No tiene permiso para acceder a Aprobar Vacaciones.', 'warning')
        return redirect(_url_inicio_portal())
    return render_template(
        'reporte_aprobar_vacaciones.html',
        **_reporte_aprobar_vacaciones_template_context(),
    )


@app.route('/reporte-saldo-vacaciones')
@login_required
def reporte_saldo_vacaciones_page():
    return render_template('reporte_saldo_vacaciones.html', **_reporte_filtros_perfil_template_context())


@app.route('/solicitud-vacaciones', methods=['GET', 'POST'])
@login_required
def solicitud_vacaciones_page():
    ensure_user_session()
    if not session.get('simple_profile'):
        flash('La solicitud de vacaciones está disponible solo para el perfil de trabajador.', 'warning')
        return redirect(url_for('configuracion_usuario'))
    company = str(getattr(current_user, 'company', None) or session.get('company') or '').strip()
    person = str(getattr(current_user, 'person', None) or session.get('person') or '').strip()
    current_year = datetime.now().year
    selected_year = str(request.form.get('controlyear') or request.args.get('controlyear') or current_year).strip()
    if not re.fullmatch(r'\d{4}', selected_year):
        selected_year = str(current_year)

    if request.method == 'POST' and request.form.get('action') == 'delete':
        solicitud_id = request.form.get('solicitud_id')
        delete_year = str(request.form.get('controlyear') or selected_year).strip()
        if not company or not person:
            flash('No se pudo identificar su usuario. Vuelva a iniciar sesión.', 'error')
        elif not solicitud_id:
            flash('Solicitud no válida.', 'warning')
        elif eliminar_solicitud_vacaciones(company, person, solicitud_id):
            flash('Solicitud eliminada correctamente.', 'success')
        else:
            flash('No se pudo eliminar la solicitud (solo pendientes pueden eliminarse).', 'warning')
        return redirect(url_for('solicitud_vacaciones_page', controlyear=delete_year))

    if request.method == 'POST':
        date_begin = str(request.form.get('date_begin') or '').strip()
        date_end = str(request.form.get('date_end') or '').strip()
        comments = str(request.form.get('comments') or '').strip()
        days = _days_between_calendar(date_begin, date_end)
        dias_totales = _dias_totales_vacaciones_ejercicio(company)
        resumen = get_resumen_solicitud_vacaciones(
            company, person, selected_year, dias_totales=dias_totales
        )
        disponibles = int(resumen.get('disponibles') or 0)

        if not company or not person:
            flash('No se pudo identificar su usuario. Vuelva a iniciar sesión.', 'error')
        elif not date_begin or not date_end:
            flash('Seleccione fecha de inicio y fin.', 'warning')
        elif days <= 0:
            flash('El rango de fechas no es válido.', 'warning')
        elif days > disponibles:
            flash('Los días solicitados superan el saldo disponible del ejercicio.', 'warning')
        elif solicitud_vacaciones_tiene_cruce(company, person, date_begin, date_end):
            flash('El rango de fechas se cruza con otra solicitud de vacaciones registrada.', 'warning')
        else:
            ok = registrar_solicitud_vacaciones(
                company=company,
                person=person,
                date_begin=date_begin,
                date_end=date_end,
                days=days,
                comments=comments,
                control_year=selected_year,
                user_id=current_user.id,
            )
            if ok:
                flash('Solicitud de vacaciones registrada correctamente.', 'success')
                return redirect(url_for('solicitud_vacaciones_page', controlyear=selected_year))
            if solicitud_vacaciones_tiene_cruce(company, person, date_begin, date_end):
                flash('El rango de fechas se cruza con otra solicitud de vacaciones registrada.', 'warning')
            else:
                flash('No se pudo registrar la solicitud. Intente nuevamente.', 'error')

    historial_rows = get_historial_solicitud_vacaciones(company, person, selected_year)
    historial = _historial_solicitud_vacaciones_items(historial_rows)
    historial_todos = get_historial_solicitud_vacaciones(company, person, None) if company and person else []

    years = sorted(
        {str(current_year - 1), str(current_year), str(current_year + 1), selected_year}
        | {str(r.get('ControlYear') or '').strip() for r in historial_todos if r.get('ControlYear')},
        reverse=True,
    )
    dias_totales = _dias_totales_vacaciones_ejercicio(company)
    resumen = get_resumen_solicitud_vacaciones(
        company, person, selected_year, dias_totales=dias_totales
    )
    rangos_vacaciones = get_rangos_solicitud_vacaciones(company, person) if company and person else []
    return render_template(
        'solicitud_vacaciones.html',
        years=years,
        selected_year=selected_year,
        kpi_total=int(resumen.get('total') or dias_totales),
        kpi_consumidos=int(resumen.get('consumidos') or 0),
        kpi_disponibles=int(resumen.get('disponibles') or 0),
        historial=historial,
        rangos_vacaciones=rangos_vacaciones,
    )


@app.route('/api/solicitud-vacaciones/ejercicio')
@login_required
def api_solicitud_vacaciones_ejercicio():
    """Resumen KPI e historial del ejercicio seleccionado (saldo independiente por ControlYear)."""
    ensure_user_session()
    if not session.get('simple_profile'):
        return jsonify({'error': 'No autorizado.'}), 403
    company = str(getattr(current_user, 'company', None) or session.get('company') or '').strip()
    person = str(getattr(current_user, 'person', None) or session.get('person') or '').strip()
    control_year = str(request.args.get('controlyear') or '').strip()
    if not re.fullmatch(r'\d{4}', control_year):
        return jsonify({'error': 'Ejercicio inválido'}), 400
    if not company or not person:
        return jsonify({'error': 'Usuario no identificado'}), 400

    dias_totales = _dias_totales_vacaciones_ejercicio(company)
    resumen = get_resumen_solicitud_vacaciones(
        company, person, control_year, dias_totales=dias_totales
    )
    historial_rows = get_historial_solicitud_vacaciones(company, person, control_year)
    historial = _historial_solicitud_vacaciones_items(historial_rows, format_dates=True)
    return jsonify({
        'controlyear': control_year,
        'kpi_total': int(resumen.get('total') or dias_totales),
        'kpi_consumidos': int(resumen.get('consumidos') or 0),
        'kpi_disponibles': int(resumen.get('disponibles') or 0),
        'historial': historial,
    })


@app.route('/reporte-documentos-personal')
@login_required
def reporte_documentos_personal_page():
    """Reporte completo con filtros (administradores / perfiles distintos de SIMPLE)."""
    ensure_user_session()
    if session.get('simple_profile'):
        flash('Use el Dashboard para descargar sus documentos.', 'info')
        return redirect(url_for('dashboard'))
    return render_template('reporte_documentos_personal.html', **_documentos_personal_template_context())


@app.route('/reporte-descansos-medicos-detalle')
@login_required
def reporte_descansos_medicos_detalle_page():
    return render_template('reporte_descansos_medicos_detalle.html')


@app.route('/procesar_planilla')
@login_required
def procesar_planilla_page():
    return render_template('procesar_planilla.html')


@app.route('/generar_boletas')
@login_required
def generar_boletas_page():
    return render_template('generar_boletas.html')


@app.route('/get_lista_boletas', methods=['POST'])
@login_required
def get_lista_boletas():
    """
    sp_pr_listadogenerarboletas_web @cia, @payrolltype, @processtype, @period, @person.

    Nota BD: el filtro para listar todos con @person = '0' debe ser
    ``(@person = '0' OR PR_EmployeePayRoll.Person = @person)``.
    Si el SP usa ``(@person = '0' AND Person = @person)``, no devolverá filas al listar todos.
    """
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or session.get('company') or '').strip()
    payroll_type = str(body.get('payroll_type') or '').strip()
    processtype = str(body.get('process') or body.get('processtype') or '').strip()
    period = _normalize_pr_period(body.get('period'))
    person = str(body.get('person') or '0').strip() or '0'

    if not cia or not payroll_type or not processtype or not period:
        return jsonify({'error': 'Faltan compañía, tipo de planilla, proceso o periodo.'}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'EXEC sp_pr_listadogenerarboletas_web @cia=?, @payrolltype=?, @processtype=?, @period=?, @person=?',
            (cia, payroll_type, processtype, period, person),
        )
        rows = _dicts_first_nonempty_resultset(cursor)
        trabajadores = []
        for r in rows:
            fi = _jsonable_value(r.get('fechaingreso'))
            fc = _jsonable_value(r.get('fechacese'))
            trabajadores.append(
                {
                    'person': str(r.get('person') or '').strip(),
                    'nombre': str(r.get('nombre') or '').strip(),
                    'email': str(r.get('email') or '').strip(),
                    'ingreso': fi if fi is not None else '',
                    'cese': fc if fc is not None else '',
                }
            )
        return jsonify(trabajadores)
    except Exception as e:
        logging.exception('get_lista_boletas')
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/preview_boleta')
@login_required
def preview_boleta():
    params = request.args
    person = str(params.get('person') or '').strip()
    try:
        pdf_buffer = generar_pdf_en_memoria(params)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logging.exception('preview_boleta')
        return jsonify({'error': str(e)}), 500
    return send_file(
        pdf_buffer,
        mimetype='application/pdf',
        as_attachment=False,
        download_name=f'boleta_{person or "preview"}.pdf',
    )


@app.route('/procesar_boletas_masivo', methods=['POST'])
@login_required
def procesar_boletas_masivo():
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or session.get('company') or '').strip()
    payroll_type = str(body.get('payroll_type') or '').strip()
    process = str(body.get('process') or '').strip()
    period = _normalize_pr_period(body.get('period'))
    modo = str(body.get('modo') or '').strip().lower()
    seleccionados = body.get('trabajadores') or []
    if modo not in ('zip', 'mail'):
        return jsonify({'error': 'Modo inválido. Use zip o mail.'}), 400
    if not isinstance(seleccionados, list) or not seleccionados:
        return jsonify({'error': 'No hay trabajadores seleccionados.'}), 400
    if not cia or not payroll_type or not process or not period:
        return jsonify({'error': 'Faltan filtros para procesar boletas.'}), 400

    ids = [str(x).strip() for x in seleccionados if str(x).strip()]
    if not ids:
        return jsonify({'error': 'No hay IDs válidos para procesar.'}), 400

    if modo == 'zip':
        company_name = str(body.get('company_name') or cia).strip()
        safe_company = re.sub(r'[^A-Za-z0-9_\\-]+', '_', company_name).strip('_') or 'compania'
        # Periodo de BD viene como yyyymmdd; pediste nombre con yyyymm.
        period_yyyymm = period[:6] if len(period) >= 6 else period
        safe_period = re.sub(r'[^A-Za-z0-9_\\-]+', '_', period_yyyymm).strip('_') or 'periodo'
        nombre_zip = f'boletas_{safe_company.lower()}_{safe_period}.zip'
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for pid in ids:
                pdf_data = generar_pdf_en_memoria(
                    {
                        'person': pid,
                        'cia': cia,
                        'payroll_type': payroll_type,
                        'process': process,
                        'period': period,
                    }
                )
                zf.writestr(f'boleta_{pid}.pdf', pdf_data.getvalue())
        memory_file.seek(0)
        return send_file(
            memory_file,
            mimetype='application/zip',
            download_name=nombre_zip,
            as_attachment=True,
        )

    # Stub controlado para modo correo (pendiente integración real de SMTP/servicio).
    return jsonify(
        {
            'status': 'pending',
            'message': 'Modo envío por Email pendiente de integración.',
            'total': len(ids),
        }
    ), 202


@app.route('/descargar_zip_boletas')
@login_required
def descargar_zip_boletas():
    ensure_user_session()
    cia = session.get('company')
    payroll_type = (request.args.get('payroll_type') or '').strip()
    processtype = (request.args.get('process') or '').strip()
    period = _normalize_pr_period(request.args.get('period'))
    company_name = (request.args.get('company_name') or '').strip()
    trabajadores_raw = (request.args.get('trabajadores') or '').strip()
    seleccionados = [x.strip() for x in trabajadores_raw.split(',') if x.strip()]

    if not (cia and payroll_type and processtype and period):
        flash('Faltan filtros para generar el ZIP de boletas.', 'warning')
        return redirect(url_for('generar_boletas_page'))

    empleados = get_listado_generar_boletas(cia, payroll_type, processtype, period, '0')
    if not empleados:
        flash('No hay boletas para procesar en este periodo.', 'warning')
        return redirect(url_for('generar_boletas_page'))

    # Si se envía selección, limita a esos códigos.
    if seleccionados:
        wanted = set(seleccionados)
        empleados = [e for e in empleados if str(e.get('person') or e.get('employeecode') or '').strip() in wanted]
        if not empleados:
            flash('La selección no contiene boletas válidas para el periodo indicado.', 'warning')
            return redirect(url_for('generar_boletas_page'))

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for emp in empleados:
            person_id = str(emp.get('person') or emp.get('employeecode') or '').strip()
            if not person_id:
                continue
            try:
                params = {
                    'cia': cia,
                    'payroll_type': payroll_type,
                    'process': processtype,
                    'period': period,
                    'person': person_id,
                }
                pdf_io = generar_pdf_en_memoria(params)
                fullname = str(emp.get('nombre') or emp.get('fullname') or '').strip()
                safe_name = re.sub(r'[^A-Za-z0-9_\\-]+', '_', fullname).strip('_')
                if not safe_name:
                    safe_name = person_id
                nombre_pdf = f'{person_id}_{safe_name}.pdf'
                zip_file.writestr(nombre_pdf, pdf_io.getvalue())
            except Exception as e:
                logging.exception('descargar_zip_boletas persona=%s', person_id)
                continue

    zip_buffer.seek(0)
    safe_company = re.sub(r'[^A-Za-z0-9_\\-]+', '_', company_name or cia).strip('_') or 'COMPANIA'
    safe_period = re.sub(r'[^A-Za-z0-9_\\-]+', '_', period).strip('_') or 'PERIODO'
    safe_payroll = re.sub(r'[^A-Za-z0-9_\\-]+', '_', payroll_type).strip('_') or 'PLANILLA'
    nombre_zip = f'Boletas_{safe_company}_{safe_period}_{safe_payroll}.zip'
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=nombre_zip,
    )


@app.route('/enviar_boletas_masivo', methods=['POST'])
@login_required
def enviar_boletas_masivo():
    data = request.get_json(silent=True) or {}
    ensure_user_session()
    cia = session.get('company')
    payroll_type = str(data.get('payroll_type') or '').strip()
    process = str(data.get('process') or '').strip()
    period = _normalize_pr_period(data.get('period'))
    seleccionados = data.get('empleados', data.get('trabajadores', []))

    if not isinstance(seleccionados, list) or not seleccionados:
        return jsonify({'error': 'Debe enviar una lista de empleados.'}), 400
    if not (cia and payroll_type and process and period):
        return jsonify({'error': 'Faltan filtros para envío de boletas.'}), 400

    # Trae email/nombre del mismo SP de listado para el periodo.
    empleados_periodo = get_listado_generar_boletas(cia, payroll_type, process, period, '0')
    by_person = {}
    for e in empleados_periodo:
        pid = str(e.get('person') or e.get('employeecode') or '').strip()
        if pid:
            by_person[pid] = e

    ids = [str(x).strip() for x in seleccionados if str(x).strip()]
    total = len(ids)
    if total == 0:
        return jsonify({'error': 'No hay códigos de empleado válidos.'}), 400

    def generar_progreso_envio():
        enviados = 0
        errores = 0
        for idx, emp_code in enumerate(ids, start=1):
            emp = by_person.get(emp_code, {})
            emp_nombre = str(emp.get('nombre') or emp.get('fullname') or emp_code).strip()
            emp_email = str(emp.get('email') or '').strip()

            if not emp_email:
                errores += 1
                motivo = 'Sin email'
                yield f"data: {json.dumps({'empleado': emp_nombre, 'codigo': emp_code, 'status': 'Error', 'detalle': motivo, 'motivo': motivo, 'actual': idx, 'total': total, 'progreso': int((idx / total) * 100)})}\n\n"
                continue

            try:
                pdf_buffer = generar_pdf_en_memoria(
                    {
                        'cia': cia,
                        'payroll_type': payroll_type,
                        'process': process,
                        'period': period,
                        'person': emp_code,
                    }
                )
                exito, msg = enviar_correo_boleta(
                    destinatario=emp_email,
                    nombre_empleado=emp_nombre,
                    periodo=period,
                    sexo=emp.get('sex', emp.get('sexo', 0)),
                    pdf_io=pdf_buffer,
                )
                if exito:
                    enviados += 1
                    status = 'Enviado'
                    detalle = msg
                    motivo = ''
                else:
                    errores += 1
                    status = 'Error'
                    detalle = msg or 'No se pudo enviar el correo.'
                    motivo = detalle
            except Exception as e:
                logging.exception('enviar_boletas_masivo persona=%s', emp_code)
                errores += 1
                status = 'Error'
                detalle = str(e)
                motivo = detalle

            yield f"data: {json.dumps({'empleado': emp_nombre, 'codigo': emp_code, 'email': emp_email, 'status': status, 'detalle': detalle, 'motivo': motivo, 'actual': idx, 'total': total, 'progreso': int((idx / total) * 100)})}\n\n"

        yield f"data: {json.dumps({'done': True, 'enviados': enviados, 'errores': errores, 'total': total})}\n\n"

    return Response(
        stream_with_context(generar_progreso_envio()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


# ==========================================
# APIS PARA SELECTORES EN CASCADA (stored procedures)
# ==========================================


@app.route('/api/selectores/companias')
@login_required
def api_companias():
    """sp_pr_selectorcompanias_web → Company, description (@cia para el resto)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_selectorcompanias_web")
        rows = cursor.fetchall()
        data = [{"id": r.Company, "text": r.description} for r in rows]
        return jsonify(data)
    except Exception:
        logging.exception("api_companias")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/selectores/planillas')
@login_required
def api_planillas():
    """sp_pr_selectorplanillas_web @cia → payrolltype, tipoplanilla"""
    ensure_user_session()
    lock = _documentos_effective_company_lock()
    cia = request.args.get('cia')
    if lock and (not cia or str(cia).strip() != lock):
        return jsonify([])
    if not cia:
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_selectorplanillas_web @cia=?", (cia,))
        rows = cursor.fetchall()
        data = [{"id": r.payrolltype, "text": r.tipoplanilla} for r in rows]
        return jsonify(data)
    except Exception:
        logging.exception("api_planillas")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/selectores/procesos')
@login_required
def api_procesos():
    """sp_pr_selectorprocesos_web @cia, @payrolltype → processtype, proceso"""
    cia = request.args.get('cia')
    payrolltype = request.args.get('payrolltype')
    if not cia or not payrolltype:
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_selectorprocesos_web @cia=?, @payrolltype=?",
            (cia, payrolltype),
        )
        rows = cursor.fetchall()
        data = [{"id": r.processtype, "text": r.proceso} for r in rows]
        return jsonify(data)
    except Exception:
        logging.exception("api_procesos")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/selectores/periodos')
@login_required
def api_periodos():
    """sp_pr_selectorperiodos_web @cia, @payrolltype, @processtype → period, periodo"""
    cia = request.args.get('cia')
    payrolltype = request.args.get('payrolltype')
    processtype = request.args.get('processtype')
    if not all([cia, payrolltype, processtype]):
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_selectorperiodos_web @cia=?, @payrolltype=?, @processtype=?",
            (cia, payrolltype, processtype),
        )
        rows = cursor.fetchall()
        data = [{"id": r.period, "text": r.periodo} for r in rows]
        return jsonify(data)
    except Exception:
        logging.exception("api_periodos")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/selectores/periodos-asig')
@login_required
def api_periodos_asig():
    """sp_pr_selectorperiodos_asig_web @cia → PRPERIOD (id), description (text)."""
    ensure_user_session()
    lock = _documentos_effective_company_lock()
    cia = request.args.get('cia')
    if lock and (not cia or str(cia).strip() != lock):
        return jsonify([])
    if not cia:
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_selectorperiodos_asig_web @cia=?",
            (cia,),
        )
        desc = cursor.description
        rows = cursor.fetchall()
        if not rows or not desc:
            return jsonify([])
        cols = [str(c[0] or '').strip().lower() for c in desc]
        data = []
        for row in rows:
            rd = {cols[i]: row[i] for i in range(len(cols))}
            pid = rd.get('prperiod')
            txt = rd.get('description')
            pid_s = str(pid).strip() if pid is not None else ''
            txt_s = str(txt).strip() if txt is not None else pid_s
            if pid_s:
                data.append({"id": pid_s, "text": txt_s})
        return jsonify(data)
    except Exception:
        logging.exception("api_periodos_asig")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/selectores/unidades')
@login_required
def api_unidades():
    """sp_pr_selectorunidades_web → ReplicationUnit, Description (status = 'A')."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_selectorunidades_web")
        rows = cursor.fetchall()
        data = []
        for r in rows:
            try:
                rid = str(r.ReplicationUnit).strip()
                txt = str(r.Description).strip()
            except Exception:
                rid = str(r[0]).strip() if len(r) > 0 else ''
                txt = str(r[1]).strip() if len(r) > 1 else rid
            if rid:
                data.append({"id": rid, "text": txt})
        return jsonify(data)
    except Exception:
        logging.exception("api_unidades")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/selectores/trabajadores')
@login_required
def api_trabajadores():
    """sp_pr_selectorpersonas_web @cia → Person, Name"""
    ensure_user_session()
    lock_cia = _documentos_effective_company_lock()
    lock_person = _documentos_effective_person_lock()
    cia = request.args.get('cia')
    if lock_cia and (not cia or str(cia).strip() != lock_cia):
        return jsonify([])
    if not cia:
        return jsonify([])
    if lock_person:
        nombre = str(session.get('simple_lock_person_name') or lock_person).strip()
        return jsonify([{"id": lock_person, "text": nombre}])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_selectorpersonas_web @cia=?", (cia,))
        rows = cursor.fetchall()
        data = [{"id": r.Person, "text": r.Name} for r in rows]
        return jsonify(data)
    except Exception:
        logging.exception("api_trabajadores")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/selectores/tipos-documento')
@login_required
def api_tipos_documento():
    """PR_tipodocWeb → Tipodocumento, name (para filtros de reportes)."""
    try:
        rows = get_tipos_documentos() or []
        data = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            rid = str(r.get('Tipodocumento') or '').strip()
            if not rid:
                continue
            txt = str(r.get('name') or rid).strip()
            data.append({'id': rid, 'text': txt})
        return jsonify(data)
    except Exception:
        logging.exception('api_tipos_documento')
        return jsonify([])


@app.route('/api/selectores/tipos-descanso-medico')
@login_required
def api_tipos_descanso_medico():
    """sp_pr_selectortipos_dm_web @cia → MedicalRestType, Description."""
    cia = request.args.get('cia')
    if not cia:
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_selectortipos_dm_web @cia=?", (cia,))
        col_names = [str(c[0]).strip() for c in (cursor.description or [])]
        rows = cursor.fetchall()
        data = []
        for row in rows:
            rd = _row_dict_from_columns(col_names, row)
            data.append(
                {
                    "id": rd.get("medicalresttype"),
                    "text": rd.get("description"),
                }
            )
        return jsonify(data)
    except Exception:
        logging.exception("api_tipos_descanso_medico")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ==========================================
# API REPORTE PRINCIPAL
# ==========================================


@app.route('/api/reportes/promedio-liquidaciones', methods=['POST'])
@login_required
def api_reporte_promedio_liq():
    """SP_PR_ReportePromedioLiquidacion @cia, @payrolltype, @period, @person (varchar)."""
    params = _report_params_from_json(request)
    if not params:
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC SP_PR_ReportePromedioLiquidacion @cia=?, @payrolltype=?, @period=?, @person=?",
            params,
        )
        columns, rows = _fetch_first_nonempty_resultset(cursor)
        if not rows:
            return jsonify([])
        data = [{col: _jsonable_value(val) for col, val in zip(columns, row)} for row in rows]
        return jsonify(data)
    except Exception:
        logging.exception("api_reporte_promedio_liq params=%s", params)
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _row_dict_lower(cursor, row):
    """Convierte una fila pyodbc en dict con claves en minúsculas."""
    if not cursor.description:
        return {}
    return {
        str(col[0]).strip().lower(): row[i]
        for i, col in enumerate(cursor.description)
    }


def _row_dict_from_columns(column_names, row):
    """Igual que _row_dict_lower pero con nombres ya capturados (tras nextset)."""
    return {
        str(column_names[i]).strip().lower(): row[i]
        for i in range(len(column_names))
    }


def _drain_all_cursor_resultsets(cursor):
    """Consume todos los lotes devueltos por un SP (SET NOCOUNT off, varios SELECT, etc.)."""
    while True:
        if cursor.description:
            try:
                cursor.fetchall()
            except Exception:
                pass
        if not cursor.nextset():
            break


def _fetch_last_query_resultset(cursor):
    """
    SPs con CREATE/INSERT/UPDATE antes del SELECT no dejan un result set en el primer lote;
    pyodbc exige no hacer fetchall() si no hay consulta. Tomamos el último lote con description.
    """
    last_cols = None
    last_rows = None
    while True:
        if cursor.description:
            last_cols = [str(c[0]).strip() for c in cursor.description]
            last_rows = cursor.fetchall()
        if not cursor.nextset():
            break
    return last_cols or [], last_rows or []


def _float_sp_cell(value):
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@app.route('/reporte-resumen-total')
@login_required
def reporte_resumen_total():
    return render_template('reporte_resumen_total.html')


@app.route('/reporte_resumen_total', methods=['POST'])
@login_required
def reporte_resumen_total_post():
    """sp_pr_reporteplame_total_web: resumen por concepto y tipo (Mensual, Semanal, …)."""
    body = request.get_json(silent=True) or {}
    cia = (body.get('cia') or '').strip()
    payroll_type = (body.get('payroll_type') or '').strip()
    period = (body.get('period') or '').strip()

    if not cia:
        return jsonify({"error": "Seleccione una compañía."}), 400
    if not payroll_type or not period:
        return jsonify({"error": "Debe indicar tipo de planilla y periodo."}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_reporteplame_total_web @cia=?, @payrolltype=?, @period=?, @person=?",
            (cia, payroll_type, period, None),
        )
        col_names, rows = _fetch_last_query_resultset(cursor)
        resumen = []
        for row in rows:
            rd = _row_dict_from_columns(col_names, row)
            mensual = _float_sp_cell(rd.get('mensual'))
            semanal = _float_sp_cell(rd.get('semanal'))
            liquida = _float_sp_cell(rd.get('liquida'))
            vacaciones = _float_sp_cell(rd.get('vacaciones'))
            cts = _float_sp_cell(rd.get('cts'))
            grati = _float_sp_cell(rd.get('grati'))
            total_fila = mensual + semanal + liquida + vacaciones + cts + grati

            tipo_raw = rd.get('tipo')
            tipo = tipo_raw.strip() if isinstance(tipo_raw, str) else (str(tipo_raw).strip() if tipo_raw is not None else '')

            pdt_val = rd.get('pdt')
            concepto_val = rd.get('concepto')

            resumen.append(
                {
                    "tipo": tipo,
                    "pdt": '' if pdt_val is None else str(pdt_val).strip(),
                    "concepto": '' if concepto_val is None else str(concepto_val).strip(),
                    "mensual": mensual,
                    "semanal": semanal,
                    "liquida": liquida,
                    "vacaciones": vacaciones,
                    "cts": cts,
                    "grati": grati,
                    "total": total_fila,
                }
            )
        return jsonify(resumen)
    except Exception as e:
        logging.exception("reporte_resumen_total_post")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/reporte_planilla_vertical', methods=['POST'])
@login_required
def reporte_planilla_vertical_post():
    """
    sp_pr_reporteplamevertical_web @cia, @payrolltype, @process, @period, @person.
    Cabeceras dinámicas desde xx_plamevertical2 + PR_Concept; datos desde xx_reporteplanilla.
    """
    body = request.get_json(silent=True) or {}
    cia = (body.get('cia') or '').strip()
    payroll_type = (body.get('payroll_type') or body.get('payrolltype') or '').strip()
    process = (body.get('process') or '').strip()
    period = _normalize_pr_period(body.get('period'))
    person = (body.get('person') or '0').strip() or '0'

    if not cia:
        return jsonify({"error": "Seleccione una compañía."}), 400
    if not payroll_type or not process or not period:
        return jsonify({"error": "Debe indicar tipo de planilla, proceso y periodo."}), 400

    static_headers_es = [
        'Código',
        'Nombre',
        'F.Ingreso',
        'F.Cese',
        'Cargo',
        'AFP',
        'C.Costo',
        'Cod.Costo',
        'Unidad',
        'TipoPago',
        'Perfil',
        'Horas',
        'Banco',
        'Num. Cuenta',
    ]
    static_keys = [
        'person',
        'name',
        'entrydate',
        'ceasedate',
        'position',
        'afp',
        'ccname',
        'costcenter',
        'unidad',
        'tipopago',
        'profile',
        'horas',
        'banco',
        'numcuenta',
    ]

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_reporteplamevertical_web @cia=?, @payrolltype=?, @process=?, @period=?, @person=?",
            (cia, payroll_type, process, period, person),
        )
        _drain_all_cursor_resultsets(cursor)

        cursor.execute(
            """
            SELECT DISTINCT UPPER(PR_Concept.PrintText) AS conceptname, PR_Concept.reporden
            FROM xx_plamevertical2
            INNER JOIN PR_Concept ON (
                xx_plamevertical2.conceptname = PR_Concept.Description
                AND PR_Concept.Company = ?
            )
            ORDER BY PR_Concept.reporden ASC, 1 ASC
            """,
            (cia,),
        )
        concept_rows = cursor.fetchall()
        conceptos_dinamicos = []
        for crow in concept_rows:
            cname = crow[0] if crow[0] is not None else ''
            cname = str(cname).strip()
            if cname:
                conceptos_dinamicos.append(cname)

        headers = list(static_headers_es) + conceptos_dinamicos
        num_concepts = len(conceptos_dinamicos)

        # Mismo SELECT que el SP (@grupo = 'N'): no usar SELECT * sobre la tabla,
        # porque position/costcenter almacenan IDs; el SP expone descripción y CCCode.
        concept_cols_sql = ", ".join(f"concept{str(i).zfill(2)}" for i in range(1, 66))
        sql_datos = f"""
            SELECT
                person,
                name,
                entrydate,
                ceasedate,
                (SELECT Description FROM PR_Position WHERE Position = xx_reporteplanilla.position) AS position,
                afp,
                (SELECT Description FROM AC_CostCenter WHERE CostCenter = xx_reporteplanilla.costcenter) AS ccname,
                (SELECT CCCode FROM AC_CostCenter WHERE CostCenter = xx_reporteplanilla.costcenter) AS costcenter,
                (SELECT Description FROM SY_ReplicationUnit
                 INNER JOIN SY_Person ON (SY_ReplicationUnit.ReplicationUnit = SY_Person.ReplicationUnit)
                 WHERE SY_Person.Person = xx_reporteplanilla.person) AS unidad,
                (SELECT CASE WHEN ISNULL(SY_Person.isrecruiter, 'N') = 'Y' THEN 'H' ELSE 'P' END
                 FROM sy_person WHERE person = xx_reporteplanilla.person) AS tipopago,
                (SELECT description FROM PR_AccountProfile
                 INNER JOIN PR_Employee ON (
                     PR_AccountProfile.AccountProfile = PR_Employee.AccountProfile
                     AND PR_AccountProfile.company = ?
                     AND PR_Employee.Person = xx_reporteplanilla.person)) AS profile,
                (SELECT SUM(hourday) FROM PR_REGISTERHOUR
                 WHERE period = ? AND Company = ? AND person = xx_reporteplanilla.person) AS horas,
                CASE WHEN (
                    SELECT ShortName FROM PR_ProcessType
                    WHERE Company = ? AND ProcessType = ?
                ) = 'CTS' THEN (
                    SELECT name FROM ERP_Bank
                    INNER JOIN PR_Employee ON (
                        ERP_Bank.Bank = PR_Employee.CTSBank
                        AND ERP_Bank.company = ?
                        AND PR_Employee.Person = xx_reporteplanilla.person)
                ) ELSE (
                    SELECT name FROM ERP_Bank
                    INNER JOIN PR_Employee ON (
                        ERP_Bank.Bank = PR_Employee.SalaryBank
                        AND ERP_Bank.company = ?
                        AND PR_Employee.Person = xx_reporteplanilla.person)
                ) END AS banco,
                CASE WHEN (
                    SELECT ShortName FROM PR_ProcessType
                    WHERE Company = ? AND ProcessType = ?
                ) = 'CTS' THEN (
                    SELECT CTSAccount FROM PR_Employee
                    WHERE PR_Employee.Person = xx_reporteplanilla.person AND PR_Employee.Company = ?
                ) ELSE (
                    SELECT salaryaccount FROM PR_Employee
                    WHERE PR_Employee.Person = xx_reporteplanilla.person AND PR_Employee.Company = ?
                ) END AS numcuenta,
                {concept_cols_sql}
            FROM xx_reporteplanilla
            ORDER BY name
        """
        params_datos = (
            cia,
            period,
            cia,
            cia,
            process,
            cia,
            cia,
            cia,
            process,
            cia,
            cia,
        )
        cursor.execute(sql_datos, params_datos)
        desc = cursor.description
        if not desc:
            return jsonify({"headers": headers, "data": []})
        col_names = [str(c[0]).strip().lower() for c in desc]
        rows = cursor.fetchall()

        resultado = []
        for row in rows:
            rd = {col_names[i]: row[i] for i in range(len(col_names))}
            fila = []
            for key in static_keys:
                fila.append(_jsonable_value(rd.get(key)))
            for i in range(num_concepts):
                cn = f"concept{str(i + 1).zfill(2)}"
                fila.append(_float_sp_cell(rd.get(cn)))
            resultado.append(fila)

        return jsonify({"headers": headers, "data": resultado})
    except Exception as e:
        logging.exception("reporte_planilla_vertical_post")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/reporte_aprobar_vacaciones', methods=['POST'])
@login_required
def reporte_aprobar_vacaciones_post():
    """sp_pr_aprobarvacaciones_web @cia, @person, @dni."""
    ensure_user_session()
    if not _usuario_perfil_general_o_minero():
        return jsonify({'error': 'No autorizado.'}), 403

    body = request.get_json(silent=True) or {}
    cia = _reporte_compania_usuario_logueado()
    if not cia:
        cia = str(body.get('cia') or '').strip()
    person = str(body.get('person') or '0').strip() or '0'
    dni = str(body.get('dni') or '').strip()

    if not cia:
        return jsonify({'error': 'No se pudo identificar la compañía del usuario.'}), 400

    headers_es = [
        'Código',
        'Nombre',
        'Ejercicio',
        'Fecha inicio',
        'Fecha fin',
        'Estado',
        'Fecha aprobación',
    ]
    keys_datos = [
        'person',
        'name',
        'controlyear',
        'datebegin',
        'dateend',
        'status',
        'approvaldate',
    ]

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            'EXEC sp_pr_aprobarvacaciones_web @cia=?, @person=?, @dni=?',
            (cia, person, dni),
        )
        rows = _dicts_first_nonempty_resultset(cursor)
        ids_aprobados = []
        for r in rows:
            if str(r.get('status') or '').strip().upper() == 'A':
                rid = r.get('id')
                if rid is not None:
                    try:
                        ids_aprobados.append(int(rid))
                    except (TypeError, ValueError):
                        pass
        mapa_sustento = obtener_sustento_drive_ids_por_solicitudes(cia, ids_aprobados)

        resultado = []
        rows_meta = []
        for r in rows:
            status_code = str(r.get('status') or '').strip().upper() or 'P'
            fila = []
            for key in keys_datos:
                val = r.get(key)
                if key == 'status':
                    fila.append(_status_vacaciones_reporte_text(val))
                elif key in ('datebegin', 'dateend', 'approvaldate'):
                    fila.append(fecha_filter(val))
                else:
                    fila.append(_jsonable_value(val))
            resultado.append(fila)
            sid = r.get('id')
            try:
                sid_int = int(sid) if sid is not None else None
            except (TypeError, ValueError):
                sid_int = None
            tiene_sustento = bool(sid_int and mapa_sustento.get(sid_int))
            rows_meta.append({
                'id': _jsonable_value(sid),
                'person': str(r.get('person') or '').strip(),
                'name': str(r.get('name') or '').strip(),
                'controlyear': str(r.get('controlyear') or '').strip(),
                'status': status_code,
                'can_upload': status_code == 'P',
                'can_download': tiene_sustento,
            })
        headers_es.append('Acciones')
        return jsonify({'headers': headers_es, 'data': resultado, 'rows_meta': rows_meta})
    except Exception as e:
        logging.exception('reporte_aprobar_vacaciones_post')
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/sustento-vacaciones/descargar')
@login_required
def descargar_sustento_vacaciones():
    """Descarga PDF de sustento desde Drive (file_id en Comments: SUSTENTO_DRIVE:...)."""
    ensure_user_session()
    json_errors = _descarga_personal_es_fetch()
    if not _usuario_perfil_general_o_minero():
        if json_errors:
            return jsonify({'error': 'No autorizado.'}), 403
        flash('No autorizado.', 'warning')
        return redirect(url_for('reporte_aprobar_vacaciones_page'))

    solicitud_id = request.args.get('solicitud_id')
    company = _reporte_compania_usuario_logueado()
    if not solicitud_id or not company:
        if json_errors:
            return jsonify({'error': 'Solicitud o compañía no indicada.'}), 400
        flash('Solicitud o compañía no indicada.', 'error')
        return redirect(url_for('reporte_aprobar_vacaciones_page'))

    drive_id = obtener_drive_file_id_sustento_vacaciones(solicitud_id, company)
    if not drive_id:
        if json_errors:
            return jsonify({'error': 'No hay sustento PDF registrado para esta solicitud.'}), 404
        flash('No hay sustento PDF registrado para esta solicitud.', 'error')
        return redirect(url_for('reporte_aprobar_vacaciones_page'))

    try:
        archivo_io, nombre_archivo, mime = _descargar_archivo_drive(drive_id)
    except Exception as e:
        logging.exception('descargar_sustento_vacaciones solicitud_id=%s', solicitud_id)
        msg = _mensaje_error_descarga_drive(e)
        code = _codigo_error_drive_para_soporte(e)
        if json_errors:
            return jsonify({'error': msg, 'code': code}), 502
        flash(msg, 'error')
        return redirect(url_for('reporte_aprobar_vacaciones_page'))

    return send_file(
        archivo_io,
        as_attachment=True,
        download_name=nombre_archivo,
        mimetype=mime,
    )


@app.route('/aprobar-vacaciones-con-sustento', methods=['POST'])
@login_required
def aprobar_vacaciones_con_sustento():
    """Sube PDF de sustento a Drive y aprueba la solicitud (status A)."""
    ensure_user_session()
    if not _usuario_perfil_general_o_minero():
        return jsonify({'error': 'No autorizado.'}), 403

    solicitud_id = request.form.get('solicitud_id')
    person_dni = str(request.form.get('person_dni') or request.form.get('person') or '').strip()
    control_year = str(request.form.get('control_year') or request.form.get('controlyear') or '').strip()
    company = _reporte_compania_usuario_logueado()
    archivo = request.files.get('sustento_pdf')

    if not solicitud_id:
        return jsonify({'error': 'Solicitud no indicada.'}), 400
    if not company:
        return jsonify({'error': 'No se pudo identificar la compañía.'}), 400
    if not archivo or not archivo.filename:
        return jsonify({'error': 'Seleccione un archivo PDF de sustento.'}), 400

    nombre_orig = str(archivo.filename or '').strip().lower()
    if not nombre_orig.endswith('.pdf'):
        return jsonify({'error': 'El sustento debe ser un archivo PDF.'}), 400

    carpeta_info = _carpeta_sustento_vacaciones_desde_env()
    folder_id = carpeta_info.get('folder_id')
    logging.info(
        'Aprobar vacaciones sustento: GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES raw=%r '
        'parents=%r solicitud_id=%s company=%s user_id=%s',
        carpeta_info.get('raw'),
        carpeta_info.get('normalized'),
        solicitud_id,
        company,
        current_user.id,
    )
    print(
        '[Aprobar vacaciones sustento] GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES '
        f"raw={carpeta_info.get('raw')!r} parents={carpeta_info.get('normalized')!r}",
        flush=True,
    )
    if not folder_id:
        return jsonify({
            'error': (
                'No hay carpeta de Drive para sustentos. En Render defina '
                'GOOGLE_DRIVE_FOLDER_SUSTENTO_VACACIONES con el ID o URL de la carpeta '
                '(ej. CONSTANCIASVAC), compartida con la service account.'
            ),
            'drive_folder_source': carpeta_info.get('source'),
            'drive_folder_raw': carpeta_info.get('raw'),
        }), 400

    try:
        nombre_drive = _nombre_archivo_sustento_vacaciones(person_dni, control_year)
        try:
            archivo.stream.seek(0)
        except Exception:
            pass
        file_id = _subir_pdf_sustento_drive(folder_id, nombre_drive, archivo.stream)
        ok = aprobar_solicitud_vacaciones_con_sustento(
            solicitud_id=solicitud_id,
            company=company,
            approval_user=current_user.id,
            drive_file_id=file_id,
        )
        if not ok:
            return jsonify({
                'error': 'No se pudo aprobar la solicitud (ya fue procesada o no existe).',
            }), 409
        return jsonify({
            'ok': True,
            'message': 'Solicitud aprobada y sustento cargado.',
            'drive_file_id': file_id,
            'file_name': nombre_drive,
        })
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logging.exception('aprobar_vacaciones_con_sustento')
        return jsonify({'error': str(e)}), 500


@app.route('/reporte_vacaciones_detalle', methods=['POST'])
@login_required
def reporte_vacaciones_detalle_post():
    """sp_pr_r019_vacationdetail_web @cia, @period, @person, @dni."""
    ensure_user_session()
    lock_cia = _documentos_effective_company_lock()
    lock_person = _documentos_effective_person_lock()
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or '').strip()
    if lock_cia:
        cia = lock_cia
    period_raw = body.get('period')
    ps = str(period_raw).strip() if period_raw is not None else ''
    if ps == '' or ps == '0':
        period = '0'
    else:
        period = _normalize_pr_period(period_raw)
    person = str(body.get('person') or '0').strip() or '0'
    if lock_person:
        person = lock_person
    dni = str(body.get('dni') or '').strip()

    if not cia:
        return jsonify({"error": "Seleccione una compañía."}), 400

    headers_es = [
        'Periodo',
        'Código',
        'Nombre',
        'Fecha inicio',
        'Fecha fin',
        'Días',
        'Año control',
        'Cargo',
    ]
    keys_datos = ['person', 'name', 'datebegin', 'dateend', 'days', 'controlyear', 'cargo']

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_r019_vacationdetail_web @cia=?, @period=?, @person=?, @dni=?",
            (cia, period, person, dni),
        )
        rows = _dicts_first_nonempty_resultset(cursor)
        resultado = []
        for r in rows:
            fila = [_fmt_periodo_yyyy_mm(r.get('prperiod'))]
            for key in keys_datos:
                val = r.get(key)
                if key == 'days' and val is not None:
                    try:
                        fila.append(int(round(float(val))))
                    except Exception:
                        fila.append(_jsonable_value(val))
                else:
                    fila.append(_jsonable_value(val))
            resultado.append(fila)
        return jsonify({"headers": headers_es, "data": resultado})
    except Exception as e:
        logging.exception("reporte_vacaciones_detalle_post")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/reporte_documentos_personal', methods=['POST'])
@login_required
def reporte_documentos_personal_post():
    """sp_pr_reportenotificaciones_web @cia, @period, @tipodoc, @person, @dni."""
    ensure_user_session()
    lock_cia = _documentos_effective_company_lock()
    lock_person = _documentos_effective_person_lock()
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or '').strip()
    if lock_cia:
        cia = lock_cia
    period_raw = body.get('period')
    ps = str(period_raw).strip() if period_raw is not None else ''
    period = '0' if ps == '' or ps == '0' else _normalize_pr_period(period_raw)
    person = str(body.get('person') or '0').strip() or '0'
    if lock_person:
        person = lock_person
    tipodoc = str(body.get('tipodoc') or body.get('tipodocumento') or '0').strip() or '0'
    dni = str(body.get('dni') or '').strip()
    modo_simple = bool(session.get('simple_profile')) or str(body.get('modo') or '').strip().lower() == 'simple'

    if not cia:
        return jsonify({"error": "Seleccione una compañía."}), 400

    if modo_simple:
        if not lock_person:
            return jsonify({"error": "No se pudo determinar el trabajador asociado a su cuenta."}), 403
        if tipodoc in ('', '0'):
            return jsonify({"error": "Seleccione un tipo de documento."}), 400
        if not _tipodoc_web_por_codigo(tipodoc):
            return jsonify({"error": "Tipo de documento no válido."}), 400
        period = '0'
        person = lock_person
        dni = ''

    try:
        rows = _documentos_personal_fetch_rows(cia, period, tipodoc, person, dni)
        payload = _documentos_personal_build_payload(
            rows,
            cia,
            modo='simple' if modo_simple else 'completo',
        )
        return jsonify(payload)
    except Exception as e:
        logging.exception("reporte_documentos_personal_post")
        return jsonify({"error": str(e)}), 500


@app.route('/documentos-personal/descargar')
@login_required
def descargar_documento_personal():
    ensure_user_session()
    lock_cia = _documentos_effective_company_lock()
    lock_person = _documentos_effective_person_lock()
    drive_id = str(request.args.get('file_id') or '').strip()
    person = str(request.args.get('person') or '').strip()
    period = str(request.args.get('period') or '').strip()
    tipodocumento = str(request.args.get('tipodocumento') or '').strip()
    cia = str(request.args.get('cia') or '').strip() or str(session.get('company') or '').strip()
    if lock_cia:
        cia = lock_cia
    if lock_person:
        person = lock_person
    json_errors = _descarga_personal_es_fetch()

    if not drive_id:
        if json_errors:
            return jsonify({'error': 'No se encontró el archivo de Google Drive (file_id vacío).'}), 400
        flash('No se encontró el archivo de Google Drive.', 'error')
        return _documentos_personal_redirect_tras_descarga()

    try:
        archivo_io, nombre_archivo, mime = _descargar_archivo_drive(drive_id)
    except Exception as e:
        logging.exception('descargar_documento_personal')
        msg = _mensaje_error_descarga_drive(e)
        code = _codigo_error_drive_para_soporte(e)
        if json_errors:
            return jsonify({'error': msg, 'code': code}), 502
        flash(msg, 'error')
        return _documentos_personal_redirect_tras_descarga()

    if cia and person and period and tipodocumento:
        if not User.usuario_omite_actualizacion_fechadescarga_descarga(current_user.id):
            ok = actualizar_fechadescarga_boleta(cia, person, tipodocumento, period)
            if not ok:
                flash('No se pudo actualizar la fecha de descarga en DocumentosBoletas.', 'error')

    return send_file(
        archivo_io,
        as_attachment=True,
        download_name=nombre_archivo,
        mimetype=mime,
    )


def _parse_fecha_reporte_saldo(raw):
    """Fecha de corte para sp_pr_reportesaldos_total_web (por defecto hoy)."""
    if raw is None or str(raw).strip() == '':
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    s = str(raw).strip()
    if 'T' in s:
        s = s.split('T')[0]
    s = s.replace('/', '-')[:10]
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d')
    except ValueError:
        try:
            return datetime.strptime(s[:10], '%d/%m/%Y')
        except ValueError:
            return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


@app.route('/reporte_saldo_vacaciones', methods=['POST'])
@login_required
def reporte_saldo_vacaciones_post():
    """sp_pr_reportesaldos_total_web @company, @person, @date, @cesados, @dni."""
    ensure_user_session()
    lock_cia = _documentos_effective_company_lock()
    lock_person = _documentos_effective_person_lock()
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or '').strip()
    if lock_cia:
        cia = lock_cia
    person = str(body.get('person') or '0').strip() or '0'
    if lock_person:
        person = lock_person
    dni = str(body.get('dni') or '').strip()
    fecha_corte = _parse_fecha_reporte_saldo(body.get('date') or body.get('fecha'))
    cesados_raw = str(body.get('cesados') or body.get('cesados_saldo') or 'T').strip().upper()
    cesados = cesados_raw if cesados_raw in ('T', 'Y', 'N') else 'T'

    if not cia:
        return jsonify({"error": "Seleccione una compañía."}), 400

    headers_es = [
        'Código',
        'Nombre',
        'Fecha ingreso',
        'Fecha cese',
        'Año control',
        'Fecha inicio',
        'Fecha fin',
        'Tomados',
        'Pendientes',
        'Vencidos',
        'Truncos',
    ]
    keys_datos = [
        'codigo',
        'nombre',
        'fechaingreso',
        'fechacese',
        'controlyear',
        'fechainicio',
        'fechafin',
        'tomados',
        'pendientes',
        'vencidos',
        'truncos',
    ]

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_reportesaldos_total_web @company=?, @person=?, @date=?, @cesados=?, @dni=?",
            (cia, person, fecha_corte, cesados, dni),
        )
        rows = _dicts_last_nonempty_resultset(cursor)
        resultado = []
        for r in rows:
            fila = []
            for key in keys_datos:
                val = r.get(key)
                if key == 'tomados' and val is not None:
                    try:
                        fila.append(int(round(float(val))))
                    except Exception:
                        fila.append(_jsonable_value(val))
                elif key in ('pendientes', 'vencidos') and val is not None:
                    try:
                        fila.append(float(val))
                    except Exception:
                        fila.append(_jsonable_value(val))
                else:
                    fila.append(_jsonable_value(val))
            resultado.append(fila)
        return jsonify({"headers": headers_es, "data": resultado})
    except Exception as e:
        logging.exception("reporte_saldo_vacaciones_post")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/reporte_descansos_medicos_detalle', methods=['POST'])
@login_required
def reporte_descansos_medicos_detalle_post():
    """sp_pr_reportesdescansos_medicos_web @cia, @payrolltype, @period, @person, @medicalresttype."""
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or '').strip()
    payroll_type = str(body.get('payroll_type') or body.get('payrolltype') or '').strip()
    period_raw = body.get('period')
    ps = str(period_raw).strip() if period_raw is not None else ''
    if ps == '' or ps == '0':
        period = '0'
    else:
        period = _normalize_pr_period(period_raw)
    person = str(body.get('person') or '0').strip() or '0'
    mrt_raw = body.get('medicalresttype')
    mrs = str(mrt_raw).strip() if mrt_raw is not None else ''
    medicalresttype = '0' if mrs == '' or mrs == '0' else mrs

    if not cia:
        return jsonify({"error": "Seleccione una compañía."}), 400
    if not payroll_type:
        return jsonify({"error": "Debe indicar tipo de planilla."}), 400

    headers_es = [
        'Periodo',
        'Código',
        'Nombre',
        'Fecha inicio',
        'Fecha fin',
        'Días',
        'Tipo de descanso',
        'CITT',
    ]
    keys_datos = ['person', 'name', 'datebegin', 'dateend', 'days', 'description', 'citt']

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_reportesdescansos_medicos_web @cia=?, @payrolltype=?, @period=?, @person=?, @medicalresttype=?",
            (cia, payroll_type, period, person, medicalresttype),
        )
        rows = _dicts_first_nonempty_resultset(cursor)
        resultado = []
        for r in rows:
            fila = [_fmt_periodo_yyyy_mm(r.get('prperiod'))]
            for key in keys_datos:
                val = r.get(key)
                if key == 'days' and val is not None:
                    try:
                        fila.append(int(round(float(val))))
                    except Exception:
                        fila.append(_jsonable_value(val))
                else:
                    fila.append(_jsonable_value(val))
            resultado.append(fila)
        return jsonify({"headers": headers_es, "data": resultado})
    except Exception as e:
        logging.exception("reporte_descansos_medicos_detalle_post")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ==========================================
# API Procesar planilla (cálculo) — SPs dedicados
# ==========================================


@app.route('/api/procesar-planilla/procesos-calculo', methods=['POST'])
@login_required
def api_procesar_planilla_procesos():
    """sp_pr_selectorprocesoscalculo_web @cia, @payrolltype → PROCESSTYPE, DESCRIPTION."""
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or '').strip()
    payrolltype = str(body.get('payrolltype') or '').strip()
    if not cia or not payrolltype:
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_selectorprocesoscalculo_web @cia=?, @payrolltype=?",
            (cia, payrolltype),
        )
        rows = _dicts_first_nonempty_resultset(cursor)
        data = [
            {
                "id": str(r.get("processtype") or "").strip(),
                "text": str(r.get("description") or "").strip(),
            }
            for r in rows
            if str(r.get("processtype") or "").strip()
        ]
        return jsonify(data)
    except Exception:
        logging.exception("api_procesar_planilla_procesos")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/api/procesar-planilla/periodos-calculo')
@login_required
def api_procesar_planilla_periodos_list():
    """sp_pr_selectorperiodocalculo_web @cia, @processtype → PRPERIOD, description (lista ordenada en SP)."""
    cia = request.args.get('cia', '').strip()
    processtype = request.args.get('processtype', '').strip()
    if not cia or not processtype:
        return jsonify([])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_selectorperiodocalculo_web @cia=?, @processtype=?",
            (cia, processtype),
        )
        rows = _dicts_first_nonempty_resultset(cursor)
        data = []
        for r in rows:
            raw = r.get("prperiod")
            pid = _normalize_pr_period(raw) or str(raw or "").strip()
            if not pid:
                continue
            data.append(
                {
                    "id": pid,
                    "text": str(r.get("description") or "").strip(),
                }
            )
        return jsonify(data)
    except Exception:
        logging.exception("api_procesar_planilla_periodos_list")
        return jsonify([])
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _fecha_tabla_json(val):
    """Serializa fecha/datetime para columnas de listados (JSON)."""
    if val is None:
        return ''
    if isinstance(val, datetime):
        return val.strftime('%d/%m/%Y')
    if isinstance(val, date):
        return val.strftime('%d/%m/%Y')
    return str(val).strip()


@app.route('/api/procesar-planilla/trabajadores-calculo', methods=['POST'])
@login_required
def api_procesar_planilla_trabajadores():
    """sp_pr_calcularplanillas_web @cia, @payrolltype, @period, @cesados, @repunit → name, person, entrydate, ceasedate…"""
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or '').strip()
    payrolltype = str(body.get('payrolltype') or '').strip()
    period = _normalize_pr_period(body.get('period'))
    cesados = str(body.get('cesados') or 'T').strip().upper()
    if cesados not in ('T', 'Y', 'N'):
        cesados = 'T'
    repunit = str(body.get('repunit') or body.get('unidad') or '0').strip()
    if not repunit:
        repunit = '0'
    if len(repunit) > 20:
        repunit = repunit[:20]
    if not cia or not payrolltype or not period:
        return jsonify({"error": "Faltan compañía, tipo de planilla o periodo."}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_calcularplanillas_web @cia=?, @payrolltype=?, @period=?, @cesados=?, @repunit=?",
            (cia, payrolltype, period, cesados, repunit),
        )
        rows = _dicts_first_nonempty_resultset(cursor)
        trabajadores = [
            {
                "person": str(r.get("person") or "").strip(),
                "name": str(r.get("name") or "").strip(),
                "entrydate": _fecha_tabla_json(r.get("entrydate")),
                "ceasedate": _fecha_tabla_json(r.get("ceasedate")),
            }
            for r in rows
            if str(r.get("person") or "").strip()
        ]
        return jsonify(trabajadores)
    except Exception as e:
        logging.exception("api_procesar_planilla_trabajadores")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/ejecutar_calculo_planilla', methods=['POST'])
@login_required
def ejecutar_calculo_planilla():
    """
    Resuelve el SP en PR_ProcessType (ProcedureName) y lo ejecuta por cada person.
    Orden de parámetros del CALL: cia, payroll_type, processtype, period, person, user_id, tc.
    """
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or session.get('company') or '').strip()
    processtype = str(body.get('processtype') or '').strip()
    payroll_type = str(body.get('payroll_type') or '').strip()
    period = _normalize_pr_period(body.get('period'))
    seleccionados = body.get('trabajadores')

    if not isinstance(seleccionados, list) or len(seleccionados) == 0:
        return jsonify({'error': 'Debe enviar una lista no vacía de trabajadores (person).'}), 400
    if not cia or not processtype or not payroll_type or not period:
        return jsonify({'error': 'Faltan compañía, tipo de planilla, proceso o periodo.'}), 400

    try:
        user_id = current_user.id
    except AttributeError:
        return jsonify({'error': 'Usuario no identificado.'}), 401

    tc = 3.0
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        _set_cursor_timeout(cursor)
        cursor.execute(
            """
            SELECT ProcedureName
            FROM PR_ProcessType
            WHERE ProcessType = ? AND Company = ?
            """,
            (processtype, cia),
        )
        row = cursor.fetchone()
        proc_raw = None
        if row:
            proc_raw = getattr(row, 'ProcedureName', None)
            if proc_raw is None and len(row) > 0:
                proc_raw = row[0]
        sp_name = _sanitize_dynamic_procedure_name(proc_raw)
        if not sp_name:
            return jsonify(
                {
                    'error': 'No se encontró un procedimiento configurado para este proceso '
                    'o el nombre del procedimiento no es válido.'
                }
            ), 400

        _drain_pyodbc_cursor(cursor)

        exitos = 0
        errores = []
        call_sql = f'{{CALL {sp_name} (?, ?, ?, ?, ?, ?, ?)}}'

        for person_id in seleccionados:
            pid = str(person_id).strip()
            if not pid:
                continue
            try:
                cursor.execute(
                    call_sql,
                    (cia, payroll_type, processtype, period, pid, user_id, tc),
                )
                _drain_pyodbc_cursor(cursor)
                conn.commit()
                exitos += 1
            except Exception as e_individual:
                if _is_transient_sql_error(e_individual):
                    logging.warning(
                        'ejecutar_calculo_planilla persona %s: error transitorio; reintentando 1 vez',
                        pid,
                    )
                    try:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        _set_cursor_timeout(cursor)
                        cursor.execute(
                            call_sql,
                            (cia, payroll_type, processtype, period, pid, user_id, tc),
                        )
                        _drain_pyodbc_cursor(cursor)
                        conn.commit()
                        exitos += 1
                        continue
                    except Exception as e_retry:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        errores.append(f'Error en {pid}: {e_retry}')
                        logging.warning(
                            'ejecutar_calculo_planilla persona %s fallo en reintento: %s',
                            pid,
                            e_retry,
                        )
                else:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    errores.append(f'Error en {pid}: {e_individual}')
                    logging.warning('ejecutar_calculo_planilla persona %s: %s', pid, e_individual)

        status = 'success' if not errores else 'partial'
        n_errores = len(errores)
        message = f'Proceso terminado. Éxitos: {exitos}, Errores: {n_errores}.'
        return jsonify(
            {
                'status': status,
                'message': message,
                'exitos': exitos,
                'errores': n_errores,
                'procesados': exitos + n_errores,
                'detalles': errores,
            }
        )
    except Exception as e:
        logging.exception('ejecutar_calculo_planilla')
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route('/ejecutar_calculo_streaming', methods=['POST'])
@login_required
def ejecutar_calculo_streaming():
    """
    Mismo orquestado que /ejecutar_calculo_planilla pero emite eventos SSE (text/event-stream)
    tras cada trabajador: data: {"progreso","actual","total"} y al final data: {"done",...}.
    """
    ensure_user_session()
    body = request.get_json(silent=True) or {}
    cia = str(body.get('cia') or session.get('company') or '').strip()
    processtype = str(body.get('processtype') or '').strip()
    payroll_type = str(body.get('payroll_type') or '').strip()
    period = _normalize_pr_period(body.get('period'))
    seleccionados = body.get('trabajadores')

    if not isinstance(seleccionados, list) or len(seleccionados) == 0:
        return jsonify({'error': 'Debe enviar una lista no vacía de trabajadores (person).'}), 400
    if not cia or not processtype or not payroll_type or not period:
        return jsonify({'error': 'Faltan compañía, tipo de planilla, proceso o periodo.'}), 400

    try:
        user_id = current_user.id
    except AttributeError:
        return jsonify({'error': 'Usuario no identificado.'}), 401

    lista = [str(x).strip() for x in seleccionados if str(x).strip()]
    total = len(lista)
    if total == 0:
        return jsonify({'error': 'No hay IDs de trabajador válidos en la lista.'}), 400

    tc = 3.0

    def generar_progreso():
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            _set_cursor_timeout(cursor)
            cursor.execute(
                """
                SELECT ProcedureName
                FROM PR_ProcessType
                WHERE ProcessType = ? AND Company = ?
                """,
                (processtype, cia),
            )
            row = cursor.fetchone()
            proc_raw = None
            if row:
                proc_raw = getattr(row, 'ProcedureName', None)
                if proc_raw is None and len(row) > 0:
                    proc_raw = row[0]
            sp_name = _sanitize_dynamic_procedure_name(proc_raw)
            if not sp_name:
                yield (
                    'data: '
                    + json.dumps(
                        {
                            'error': 'No se encontró un procedimiento configurado para este proceso '
                            'o el nombre del procedimiento no es válido.'
                        }
                    )
                    + '\n\n'
                )
                return

            _drain_pyodbc_cursor(cursor)

            exitos = 0
            errores = []
            call_sql = f'{{CALL {sp_name} (?, ?, ?, ?, ?, ?, ?)}}'

            for index, pid in enumerate(lista):
                # Heartbeat previo para mantener vivo el stream detrás de proxies.
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "actual": index + 1,
                            "total": total,
                            "progreso": int((index / total) * 100),
                            "person": pid,
                            "stage": "start",
                        }
                    )
                    + "\n\n"
                )
                try:
                    cursor.execute(
                        call_sql,
                        (cia, payroll_type, processtype, period, pid, user_id, tc),
                    )
                    _drain_pyodbc_cursor(cursor)
                    conn.commit()
                    exitos += 1
                    evento = {
                        'progreso': int(((index + 1) / total) * 100),
                        'actual': index + 1,
                        'total': total,
                    }
                except Exception as e_individual:
                    if _is_transient_sql_error(e_individual):
                        logging.warning(
                            'ejecutar_calculo_streaming persona %s: error transitorio; reintentando 1 vez',
                            pid,
                        )
                        try:
                            try:
                                conn.close()
                            except Exception:
                                pass
                            conn = get_db_connection()
                            cursor = conn.cursor()
                            _set_cursor_timeout(cursor)
                            cursor.execute(
                                call_sql,
                                (cia, payroll_type, processtype, period, pid, user_id, tc),
                            )
                            _drain_pyodbc_cursor(cursor)
                            conn.commit()
                            exitos += 1
                            evento = {
                                'progreso': int(((index + 1) / total) * 100),
                                'actual': index + 1,
                                'total': total,
                            }
                        except Exception as e_retry:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            msg = str(e_retry)
                            errores.append(f'Error en {pid}: {msg}')
                            logging.warning(
                                'ejecutar_calculo_streaming persona %s fallo en reintento: %s',
                                pid,
                                e_retry,
                            )
                            evento = {
                                'progreso': int(((index + 1) / total) * 100),
                                'actual': index + 1,
                                'total': total,
                                'detalle': msg,
                                'person': pid,
                            }
                    else:
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        msg = str(e_individual)
                        errores.append(f'Error en {pid}: {msg}')
                        logging.warning('ejecutar_calculo_streaming persona %s: %s', pid, e_individual)
                        evento = {
                            'progreso': int(((index + 1) / total) * 100),
                            'actual': index + 1,
                            'total': total,
                            'detalle': msg,
                            'person': pid,
                        }

                yield f'data: {json.dumps(evento)}\n\n'

            yield (
                'data: '
                + json.dumps(
                    {
                        'done': True,
                        'exitos': exitos,
                        'errores': len(errores),
                        'detalles': errores,
                    }
                )
                + '\n\n'
            )
        except Exception as e:
            logging.exception('ejecutar_calculo_streaming')
            yield f'data: {json.dumps({"error": str(e)})}\n\n'
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return Response(
        generar_progreso(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


# --- Rutas legacy (intranet): recuperar desde control de versiones al implementar Planillas ---
#
# @app.route('/datos-personales') → datos_personales
# @app.route('/resumen-ausencias') → resumen_ausencias
# @app.route('/solicitud-permisos') → solicitud_permisos
# @app.route('/documentos-personales') → documentos_personales
# @app.route('/descargar-archivo/<filename>') → descargar_archivo
# @app.route('/solicitudes-pendientes') → solicitudes_pendientes
# @app.route('/api/eventos') → api_eventos
# Helpers: fetch_pdf_file, get_sftp_client; imports: requests, paramiko, pdfkit, pyodbc, openpyxl, sendgrid, etc.

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
