import logging
import os
import pyodbc
import platform
from datetime import date, datetime
from flask_login import UserMixin
from dotenv import load_dotenv

_logger_db = logging.getLogger(__name__)

# Cargar variables de entorno desde .env
load_dotenv()


class DatabaseConfig:
    """Configuración de conexión a SQL Server"""
    
    @staticmethod
    def get_connection_string(driver_override=None):
        """Construye la cadena de conexión a SQL Server"""
        # server = '179.61.14.224,1433'
        # database = 'hm_ultra2'
        # username = 'sa'
        # password = 'HMplanillas2020'

        server = os.getenv('SQL_SERVER')
        database = os.getenv('SQL_DATABASE')
        username = os.getenv('SQL_USER')
        password = os.getenv('SQL_PASSWORD')


        print(f"DEBUG: Intentando conectar a SERVER: {server} | DB: {database}")
        

        if driver_override:
            driver = driver_override
        elif platform.system() == 'Windows':
            driver = '{SQL Server}'
        else:
            # En contenedores Linux modernos (Render/Debian 12) suele estar msodbcsql18.
            driver = 'ODBC Driver 18 for SQL Server'

        print(f"DEBUG: Intentando conectar a [{server}] usando el driver: {driver}")

        connection_string = (
            f'DRIVER={driver};'
            f'SERVER={server};'
            f'DATABASE={database};'
            f'UID={username};'
            f'PWD={password};'
            'Encrypt=no;'
            'TrustServerCertificate=yes;' # <--- ESTO EVITA EL ERROR 53 EN MUCHOS CASOS
            'Connection Timeout=10;'
        )

           
        
        return connection_string
    
    @staticmethod
    def _installed_odbc_drivers():
        try:
            return [d.strip() for d in pyodbc.drivers() if d and str(d).strip()]
        except Exception:
            return []

    @staticmethod
    def _linux_odbc_driver_candidates():
        """Solo drivers realmente instalados (evita intentar ODBC 17 en Render/Docker)."""
        installed = set(DatabaseConfig._installed_odbc_drivers())
        wishlist = [
            os.getenv('SQL_ODBC_DRIVER', '').strip(),
            'ODBC Driver 18 for SQL Server',
            'ODBC Driver 17 for SQL Server',
        ]
        candidates = []
        seen = set()
        for drv in wishlist:
            if drv and drv in installed and drv not in seen:
                seen.add(drv)
                candidates.append(drv)
        if not candidates:
            for drv in sorted(installed):
                if 'SQL Server' in drv and drv not in seen:
                    seen.add(drv)
                    candidates.append(drv)
        return candidates

    @staticmethod
    def _is_missing_odbc_driver_error(exc):
        msg = str(exc).lower()
        return (
            "can't open lib" in msg
            or 'file not found' in msg
            or 'driver manager' in msg
            or 'driver not found' in msg
        )

    @staticmethod
    def get_connection():
        """Crea y retorna una conexión a SQL Server"""
        if platform.system() == 'Windows':
            try:
                return pyodbc.connect(DatabaseConfig.get_connection_string())
            except Exception as e:
                print(f"Error al conectar con SQL Server: {e}")
                raise

        candidates = DatabaseConfig._linux_odbc_driver_candidates()
        if not candidates:
            installed = DatabaseConfig._installed_odbc_drivers()
            raise RuntimeError(
                'No hay driver ODBC para SQL Server instalado. '
                f'Drivers detectados: {installed or "(ninguno)"}'
            )

        last_error = None
        for drv in candidates:
            try:
                return pyodbc.connect(DatabaseConfig.get_connection_string(driver_override=drv))
            except Exception as e:
                last_error = e
                print(f"DEBUG: Falló conexión con driver '{drv}': {e}")
                if not DatabaseConfig._is_missing_odbc_driver_error(e):
                    raise

        print(f"Error al conectar con SQL Server: {last_error}")
        raise last_error


def get_db_connection():
    """Conexión pyodbc reutilizable (APIs, reportes)."""
    return DatabaseConfig.get_connection()


def _parse_fecha_sql(val):
    """Normaliza fecha devuelta por pyodbc (date, datetime o str)."""
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val).strip()
    if not s:
        return None
    if 'T' in s:
        s = s.split('T', 1)[0]
    s = s.replace('/', '-')[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ('%d-%m-%Y', '%m-%d-%Y', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def get_fecha_hoy_sql():
    """Fecha calendario según GETDATE() de SQL Server (misma referencia que la BD)."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT CONVERT(date, GETDATE())')
        row = cur.fetchone()
        if row:
            return _parse_fecha_sql(row[0])
    except Exception as exc:
        _logger_db.warning('get_fecha_hoy_sql: %s', exc)
    finally:
        if conn:
            conn.close()
    return None


def insertar_documento_minero(datos):
    """
    Inserta un registro en DocumentosMineria si NombreArchivo aún no existe.

    Args:
        datos: dict con claves tipo, periodo, dni, nombre, archivo_original

    Returns:
        True si se insertó una fila nueva, False si ya existía (duplicado) o error.
    """
    conn = None
    try:
        archivo = (datos.get("archivo_original") or "").strip()
        if not archivo:
            return False
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
        INSERT INTO dbo.DocumentosMineria (Tipo, Periodo, DNI, NombreEmpleado, NombreArchivo)
        SELECT ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM dbo.DocumentosMineria WHERE NombreArchivo = ?
        )
        """
        cursor.execute(
            query,
            (
                (datos.get("tipo") or "").strip(),
                (datos.get("periodo") or "").strip(),
                (datos.get("dni") or "").strip(),
                (datos.get("nombre") or "").strip(),
                archivo,
                archivo,
            ),
        )
        inserted = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        return inserted
    except Exception as e:
        print(f"Error en insertar_documento_minero: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


_MERGE_SQL_DOCUMENTOS_BOLETAS = """
MERGE dbo.DocumentosBoletas AS tgt
USING (
    SELECT
        ? AS DNI,
        ? AS Periodo,
        ? AS TipoDocumento,
        ? AS NombreTrabajador,
        ? AS NombreArchivoOriginal,
        ? AS DriveFileID
) AS src
ON (
    tgt.DNI = src.DNI
    AND tgt.Periodo = src.Periodo
    AND ISNULL(tgt.TipoDocumento, '') = ISNULL(src.TipoDocumento, '')
)
WHEN MATCHED THEN
    UPDATE SET
        tgt.NombreTrabajador = src.NombreTrabajador,
        tgt.NombreArchivoOriginal = src.NombreArchivoOriginal,
        tgt.DriveFileID = src.DriveFileID,
        tgt.FechaSincronizacion = GETDATE()
WHEN NOT MATCHED THEN
    INSERT (
        DNI, Periodo, TipoDocumento, NombreTrabajador,
        NombreArchivoOriginal, DriveFileID, FechaSincronizacion
    )
    VALUES (
        src.DNI, src.Periodo, src.TipoDocumento, src.NombreTrabajador,
        src.NombreArchivoOriginal, src.DriveFileID, GETDATE()
    );
"""


def _stats_metadata_drive_vacio():
    return {
        "procesados": 0,
        "omitidos_formato": 0,
        "omitidos_largo": 0,
        "sin_id": 0,
        "ok": 0,
    }


def _es_error_truncado_sql(exc):
    """SQL Server 8152 / ODBC 22001: dato más largo que la columna."""
    if exc is None:
        return False
    if isinstance(exc, pyodbc.DataError):
        msg = str(exc).lower()
        if "22001" in str(exc) or "8152" in msg or "truncat" in msg:
            return True
    msg = str(exc).lower()
    return "8152" in msg or "truncat" in msg or "22001" in msg


def _acumular_stats_metadata_drive(total, delta):
    for k in total:
        total[k] += delta.get(k, 0)


def procesar_un_item_metadata_drive(cursor, item):
    """
    Un archivo de Drive contra DocumentosBoletas (un MERGE).
    Retorna deltas {procesados, omitidos_formato, sin_id, ok} (0/1 en cada clave relevante).
    """
    delta = _stats_metadata_drive_vacio()
    delta["procesados"] = 1

    nombre_archivo = str((item or {}).get("name") or "").strip()
    drive_id = str((item or {}).get("id") or "").strip()

    if not drive_id:
        delta["sin_id"] = 1
        return delta

    base = nombre_archivo[:-4] if nombre_archivo.lower().endswith(".pdf") else nombre_archivo
    base = base.strip()
    # TIPO_PERIODO_DNI_Nombre → al menos 3 guiones bajos (4 segmentos mínimo)
    if base.count("_") < 3:
        delta["omitidos_formato"] = 1
        return delta

    partes = base.split("_")
    if len(partes) < 4:
        delta["omitidos_formato"] = 1
        return delta

    tipo = str(partes[0]).strip()
    periodo = str(partes[1]).strip()
    dni = str(partes[2]).strip()
    nombre_trabajador = " ".join(str(p).strip() for p in partes[3:] if str(p).strip()).strip()

    if not (tipo and periodo and dni):
        delta["omitidos_formato"] = 1
        return delta

    try:
        cursor.execute(
            _MERGE_SQL_DOCUMENTOS_BOLETAS,
            (dni, periodo, tipo, nombre_trabajador, nombre_archivo, drive_id),
        )
    except pyodbc.DataError as e:
        if _es_error_truncado_sql(e):
            delta["omitidos_largo"] = 1
            _logger_db.warning(
                "Sync Drive: nombre demasiado largo para BD (omitido): %r "
                "(dni=%r periodo=%r tipo=%r len_nombre=%s len_archivo=%s)",
                nombre_archivo,
                dni,
                periodo,
                tipo,
                len(nombre_trabajador),
                len(nombre_archivo),
            )
            return delta
        raise
    delta["ok"] = 1
    return delta


def sincronizar_metadata_drive(lista_archivos):
    """
    Sincroniza metadata de archivos PDF de Google Drive contra dbo.DocumentosBoletas.

    Reglas:
    - Nombre esperado: TIPO_PERIODO_DNI_Nombre.pdf (al menos 3 guiones bajos en el nombre base)
    - Clave de negocio: (DNI, Periodo, TipoDocumento)
    - Si existe: actualiza DriveFileID / Nombre / NombreArchivoOriginal / FechaSincronizacion
    - Si no existe: inserta registro nuevo.

    Args:
        lista_archivos: iterable de dicts con al menos {'name': str, 'id': str}

    Returns:
        dict con contadores: {procesados, omitidos_formato, sin_id, ok}
    """
    conn = None
    stats = _stats_metadata_drive_vacio()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        for item in (lista_archivos or []):
            d = procesar_un_item_metadata_drive(cursor, item)
            _acumular_stats_metadata_drive(stats, d)

        conn.commit()
        cursor.close()
        return stats
    except Exception as e:
        print(f"Error en sincronizar_metadata_drive: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return stats
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def sincronizar_metadata_drive_lote(cursor, lista_archivos, stats_acumulado=None):
    """
    Procesa un subconjunto de archivos usando un cursor ya abierto (sin commit).
    Actualiza stats_acumulado si se pasa un dict mutable.
    """
    stats = _stats_metadata_drive_vacio()
    for item in (lista_archivos or []):
        d = procesar_un_item_metadata_drive(cursor, item)
        _acumular_stats_metadata_drive(stats, d)
        if stats_acumulado is not None:
            _acumular_stats_metadata_drive(stats_acumulado, d)
    return stats


def ejecutar_sp_updatecompany_documentos_boletas():
    """
    Ejecuta sp_pr_updatecompany para actualizar la columna company de DocumentosBoletas.

    Returns:
        (True, mensaje) si ejecuta OK, (False, mensaje_error) si falla.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_updatecompany")
        conn.commit()
        cursor.close()
        return True, "Company actualizado en DocumentosBoletas."
    except Exception as e:
        print(f"Error en ejecutar_sp_updatecompany_documentos_boletas: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, f"No se pudo ejecutar sp_pr_updatecompany: {e}"
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_ruta_documentos_usuario(user_id):
    """
    Ruta de carpeta de documentos (PDF) configurada en SY_User.RutaDocumentos.
    Retorna string sin espacios extremos, o None si no hay valor.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT RutaDocumentos FROM SY_User WHERE UserID = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        if not row or row[0] is None:
            return None
        ruta = str(row[0]).strip()
        return ruta if ruta else None
    except Exception as e:
        print(f"Error en get_ruta_documentos_usuario: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def update_ruta_documentos_usuario(user_id, ruta):
    """
    Actualiza SY_User.RutaDocumentos. Cadena vacía guarda NULL (se usa ruta por defecto del sistema).

    Returns:
        (True, mensaje) o (False, mensaje_error)
    """
    conn = None
    try:
        ruta_limpia = (ruta or "").strip()
        valor_sql = ruta_limpia if ruta_limpia else None
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE SY_User SET RutaDocumentos = ? WHERE UserID = ?",
            (valor_sql, user_id),
        )
        if cursor.rowcount < 1:
            cursor.close()
            return False, "No se encontró el usuario o no hubo cambios."
        conn.commit()
        cursor.close()
        return True, "Ruta de documentos guardada correctamente."
    except Exception as e:
        print(f"Error en update_ruta_documentos_usuario: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, f"Error al guardar: {e}"
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_logoweb_empresa(company_id):
    """
    Nombre de archivo del logo web de la compañía (columna logoweb en PR_mapping2).
    Los archivos viven en static/img/logos/.
  """
    if not company_id:
        return None
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT logoweb FROM PR_mapping2 WHERE company = ?",
            (str(company_id).strip(),),
        )
        row = cursor.fetchone()
        if not row or row[0] is None:
            return None
        name = str(row[0]).strip()
        # Quitar ruta accidental (solo nombre de archivo en static/img/logos/)
        if name:
            name = os.path.basename(name.replace('\\', '/'))
        return name or None
    except Exception as e:
        print(f"Error en get_logoweb_empresa: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_config_empresa(company_id):
    """
    Obtiene nombres de archivo de logo/firma para la compañía.
    Retorna tupla (LogoNombre, FirmaNombre) o None.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT LogoNombre, FirmaNombre FROM PR_mapping2 WHERE company = ?",
            (company_id,),
        )
        row = cursor.fetchone()
        return row
    except Exception as e:
        print(f"Error en get_config_empresa: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# --- Acceso temporal (eliminar cuando el usuario exista en SY_User) ---
TEMP_USER_ID = '__TEMP_AHORNA__'
_TEMP_LOGIN_USERS = {
    'ahorna': {
        'password': 'ahorna',
        'nombre': 'Asdrubal Horna Quintana',
    },
}


class User(UserMixin):
    """Clase de usuario para Flask-Login"""

    _SQL_LOGIN_GENERAL = """
        SELECT
            u.UserID,
            p.Name,
            p.email,
            p.Name, c.Company, p.person
        FROM SY_User u
        INNER JOIN SY_Person p ON p.UserID = u.UserID
        INNER JOIN SY_Company c ON (p.Company = c.Company)
        INNER JOIN SY_UserProfile up ON up.UserID = u.UserID
        INNER JOIN PR_mapping2 M ON (c.Company = M.company)
        WHERE u.UserID = ? AND u.PasswordWeb = ?
        """

    _SQL_LOGIN_EMPLEADO = """
        SELECT
            u.UserID,
            p.Name,
            p.email,
            p.Name, c.Company, p.person
        FROM SY_User u
        INNER JOIN SY_Person p ON p.UserID = u.UserID
        INNER JOIN PR_Employee E ON (p.Person = E.Person AND E.Status = 'N')
        INNER JOIN SY_Company c ON (E.Company = c.Company)
        INNER JOIN SY_UserProfile up ON up.UserID = u.UserID
        INNER JOIN PR_mapping2 M ON (c.Company = M.company)
        WHERE u.UserID = ? AND u.PasswordWeb = ?
        """

    _SQL_USER_BY_ID_GENERAL = """
        SELECT
            u.UserID,
            p.Name,
            p.email,
            p.Name, c.Company, p.person
        FROM SY_User u
        INNER JOIN SY_Person p ON p.UserID = u.UserID
        INNER JOIN SY_Company c ON (p.Company = c.Company)
        INNER JOIN SY_UserProfile up ON up.UserID = u.UserID
        INNER JOIN PR_mapping2 M ON (c.Company = M.company)
        WHERE u.UserID = ?
        """

    _SQL_USER_BY_ID_EMPLEADO = """
        SELECT
            u.UserID,
            p.Name,
            p.email,
            p.Name, c.Company, p.person
        FROM SY_User u
        INNER JOIN SY_Person p ON p.UserID = u.UserID
        INNER JOIN PR_Employee E ON (p.Person = E.Person AND E.Status = 'N')
        INNER JOIN SY_Company c ON (E.Company = c.Company)
        INNER JOIN SY_UserProfile up ON up.UserID = u.UserID
        INNER JOIN PR_mapping2 M ON (c.Company = M.company)
        WHERE u.UserID = ?
        """

    def __init__(self, user_id, username, email=None, nombre=None, company=None, person=None):
        self.id = user_id
        self.username = username
        self.email = email
        self.nombre = nombre
        self.company = company
        self.person = person

    @staticmethod
    def _tiene_perfil_general(cursor, userid):
        """True si el usuario tiene perfil GENERAL (acceso portal sin empleado activo)."""
        cursor.execute(
            """
            SELECT 1
            FROM SY_User u
            INNER JOIN SY_UserProfile up ON u.UserID = up.UserID
                AND up.Profile = 'GENERAL'
            WHERE u.UserID = ?
            """,
            (userid,),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _tiene_perfil_minero(cursor, userid):
        """True si el usuario tiene perfil MINERO."""
        cursor.execute(
            """
            SELECT 1
            FROM SY_User u
            INNER JOIN SY_UserProfile up ON u.UserID = up.UserID
                AND up.Profile = 'MINERO'
            WHERE u.UserID = ?
            """,
            (userid,),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _tiene_perfil_simple(cursor, userid):
        """True si el usuario tiene perfil SIMPLE."""
        cursor.execute(
            """
            SELECT 1
            FROM SY_User u
            INNER JOIN SY_UserProfile up ON u.UserID = up.UserID
                AND up.Profile = 'SIMPLE'
            WHERE u.UserID = ?
            """,
            (userid,),
        )
        return cursor.fetchone() is not None

    @staticmethod
    def get_simple_documentos_scope(userid):
        """
        Si el usuario tiene perfil SIMPLE, devuelve compañía, código de persona y nombre
        para fijar filtros en Documentos del personal.
        """
        try:
            conn = DatabaseConfig.get_connection()
            cursor = conn.cursor()
            if not User._tiene_perfil_simple(cursor, userid):
                cursor.close()
                conn.close()
                return None
            sql_empleado = """
                SELECT TOP 1 E.Company, p.Person, p.Name
                FROM SY_User u
                INNER JOIN SY_UserProfile ups ON ups.UserID = u.UserID AND ups.Profile = 'SIMPLE'
                INNER JOIN SY_Person p ON p.UserID = u.UserID
                INNER JOIN PR_Employee E ON (p.Person = E.Person AND E.Status = 'N')
                INNER JOIN SY_Company c ON (E.Company = c.Company)
                INNER JOIN PR_mapping2 M ON (c.Company = M.company)
                WHERE u.UserID = ?
                """
            cursor.execute(sql_empleado, (userid,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if not row or row[0] is None or row[1] is None:
                return None
            company = str(row[0]).strip()
            person = str(row[1]).strip()
            name = str(row[2]).strip() if row[2] is not None else person
            if not company or not person:
                return None
            return {'company': company, 'person': person, 'person_name': name}
        except Exception as e:
            print(f"Error en get_simple_documentos_scope: {e}")
            return None

    @staticmethod
    def get_minero_lock_company(userid):
        """
        Si el usuario tiene perfil MINERO, devuelve la compañía (misma lógica que el login:
        GENERAL → compañía vía SY_Person/SY_Company; si no, empleado activo vía PR_Employee).
        Si no es MINERO o no hay fila válida, devuelve None.
        """
        try:
            conn = DatabaseConfig.get_connection()
            cursor = conn.cursor()
            if not User._tiene_perfil_minero(cursor, userid):
                cursor.close()
                conn.close()
                return None
            sql_general = """
                SELECT TOP 1 c.Company
                FROM SY_User u
                INNER JOIN SY_UserProfile upm ON upm.UserID = u.UserID AND upm.Profile = 'MINERO'
                INNER JOIN SY_Person p ON p.UserID = u.UserID
                INNER JOIN SY_Company c ON (p.Company = c.Company)
                INNER JOIN SY_UserProfile up ON up.UserID = u.UserID
                INNER JOIN PR_mapping2 M ON (c.Company = M.company)
                WHERE u.UserID = ?
                """
            sql_empleado = """
                SELECT TOP 1 E.Company
                FROM SY_User u
                INNER JOIN SY_UserProfile upm ON upm.UserID = u.UserID AND upm.Profile = 'MINERO'
                INNER JOIN SY_Person p ON p.UserID = u.UserID
                INNER JOIN PR_Employee E ON (p.Person = E.Person AND E.Status = 'N')
                INNER JOIN SY_Company c ON (E.Company = c.Company)
                INNER JOIN SY_UserProfile up ON up.UserID = u.UserID
                INNER JOIN PR_mapping2 M ON (c.Company = M.company)
                WHERE u.UserID = ?
                """
            if User._tiene_perfil_general(cursor, userid):
                cursor.execute(sql_general, (userid,))
            else:
                cursor.execute(sql_empleado, (userid,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if not row or row[0] is None:
                return None
            return str(row[0]).strip() or None
        except Exception as e:
            print(f"Error en get_minero_lock_company: {e}")
            return None

    @staticmethod
    def usuario_omite_actualizacion_fechadescarga_descarga(userid):
        """
        True solo para GENERAL o MINERO: no actualizar fechadescarga al descargar
        en Documentos del personal.

        Perfil SIMPLE y cualquier otro sí actualizan fechadescarga (comportamiento
        de empleado que descarga su documento).
        """
        try:
            conn = DatabaseConfig.get_connection()
            cursor = conn.cursor()
            gen = User._tiene_perfil_general(cursor, userid)
            minero = User._tiene_perfil_minero(cursor, userid)
            cursor.close()
            conn.close()
            return bool(gen or minero)
        except Exception as e:
            print(f"Error en usuario_omite_actualizacion_fechadescarga_descarga: {e}")
            return False

    @staticmethod
    def is_temp_user(user_id):
        return str(user_id) == TEMP_USER_ID

    @staticmethod
    def _validar_usuario_temporal(username, password):
        """Login temporal sin base de datos (solo desarrollo/pruebas)."""
        key = (username or '').strip().lower()
        cfg = _TEMP_LOGIN_USERS.get(key)
        if not cfg or (password or '') != cfg['password']:
            return None
        login_name = (username or '').strip() or key
        return User(
            TEMP_USER_ID,
            login_name,
            email=None,
            nombre=cfg['nombre'],
            company=None,
            person=None,
        )

    @staticmethod
    def validate_user(username, password):
        """
        Valida las credenciales del usuario contra la base de datos.

        Si existe fila en SY_User + SY_UserProfile con Profile = 'GENERAL' para el UserID,
        se valida con la consulta sin PR_Employee; en caso contrario se usa la consulta
        original (empleado activo Status = 'N').
        """
        temp_user = User._validar_usuario_temporal(username, password)
        if temp_user:
            return temp_user

        try:
            conn = DatabaseConfig.get_connection()
            cursor = conn.cursor()

            print(f"DEBUG: Intentando login con usuario: '{username}'")

            if User._tiene_perfil_general(cursor, username):
                cursor.execute(User._SQL_LOGIN_GENERAL, (username, password))
            else:
                cursor.execute(User._SQL_LOGIN_EMPLEADO, (username, password))

            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if row:
                user_id, username_db, email, nombre = row[0], row[1], row[2], row[3]
                company = str(row[4]).strip() if row[4] is not None else None
                person = str(row[5]).strip() if row[5] is not None else None
                return User(user_id, username_db, email, nombre, company=company, person=person)

            return None

        except Exception as e:
            print(f"Error al validar usuario: {e}")
            return None

    @staticmethod
    def get_user_by_id(user_id):
        """
        Obtiene un usuario por su ID (misma regla GENERAL vs empleado que validate_user).
        """
        if User.is_temp_user(user_id):
            cfg = _TEMP_LOGIN_USERS.get('ahorna', {})
            return User(
                TEMP_USER_ID,
                'ahorna',
                email=None,
                nombre=cfg.get('nombre', 'Asdrubal Horna Quintana'),
                company=None,
                person=None,
            )

        try:
            conn = DatabaseConfig.get_connection()
            cursor = conn.cursor()

            if User._tiene_perfil_general(cursor, user_id):
                cursor.execute(User._SQL_USER_BY_ID_GENERAL, (user_id,))
            else:
                cursor.execute(User._SQL_USER_BY_ID_EMPLEADO, (user_id,))

            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if row:
                user_id_db, username, email, nombre = row[0], row[1], row[2], row[3]
                company = str(row[4]).strip() if row[4] is not None else None
                person = str(row[5]).strip() if row[5] is not None else None
                return User(user_id_db, username, email, nombre, company=company, person=person)

            return None

        except Exception as e:
            print(f"Error al obtener usuario: {e}")
            return None


def get_datos_usuario_web(userid):
    """
    Ejecuta el SP sp_pr_datosusuario_web y retorna los datos del usuario.

    Args:
        userid: UserID / código de acceso (ej: current_user.id)

    Returns:
        dict con las columnas del SP o None si no hay resultado.
        Incluye entre otros: primerapellido, segundoapellido, nombres, TipoDocumento,
        NroDocumento, LugarNacimiento, FechaNacimiento, TelefonoFijo, Movil, email,
        Direccion, distrito, provincia, departamento, Fotografia, company, person,
        NivelInstruccion, Institucion, carrera, tipoempleado, FechaIngreso, tipocontrato,
        Regimenenpension, cussp, AsignacionFamiliar, Afpmixta, cargo, BancoSalario, etc.
    """
    cols_fallback = [
        'primerapellido', 'segundoapellido', 'nombres', 'TipoDocumento', 'NroDocumento',
        'LugarNacimiento', 'FechaNacimiento', 'TelefonoFijo', 'Movil', 'email', 'Direccion',
        'distrito', 'provincia', 'departamento', 'Fotografia', 'company', 'person',
        'NivelInstruccion', 'Institucion', 'carrera', 'tipoempleado', 'FechaIngreso',
        'tipocontrato', 'Regimenenpension', 'cussp', 'AsignacionFamiliar', 'Afpmixta',
        'BancoSalario', 'CuentaSalario', 'BancoCTS', 'CuentaCTS'
    ]
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_datosusuario_web ?", (userid,))
        row = cursor.fetchone()
        columns = [c[0] for c in cursor.description] if cursor.description else cols_fallback
        cursor.close()
        conn.close()

        print(f'Buscando datos para: {userid}')

        if not row:
            return None
        data = dict(zip(columns, row))
        cia = data.get('company')
        if cia is not None and str(cia).strip():
            data['logoweb'] = get_logoweb_empresa(cia)
        return data
    except Exception as e:
        print(f"Error en get_datos_usuario_web: {e}")
        return None


def cambiar_password(userid, clave_ant, clave_nueva):
    """
    Llama al SP sp_pr_CambiarPassword_web
    Retorna: (True, "mensaje") si es OK, o (False, "Mensaje de error") si es KO.
    """
    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_CambiarPassword_web @userid=?, @clave_ant=?, @clave_nueva=?",
            (userid, clave_ant, clave_nueva)
        )
        # El SP hace UPDATE y luego SELECT; el driver puede devolver primero "rows affected".
        # Saltar a la result set del SELECT si fetchone falla con "No results".
        row = None
        try:
            row = cursor.fetchone()
        except Exception as e:
            if "No results" in str(e) and "not a query" in str(e):
                if cursor.nextset():
                    row = cursor.fetchone()
        cursor.close()
        if row:
            # SP devuelve: col0=resultado ('OK'/'KO'), col1=Mensaje
            resultado = (row[0] or '').strip().upper() if len(row) > 0 else ''
            mensaje = (row[1] or '').strip() if len(row) > 1 else ''
            if resultado == 'OK':
                conn.commit()
                return True, "Contraseña actualizada correctamente."
            return False, mensaje or "Error al cambiar la contraseña."
        return False, "Error desconocido al procesar la solicitud."
    except Exception as e:
        print(f"Error en cambiar_password: {e}")
        import traceback
        traceback.print_exc()
        return False, f"Error: {str(e)}"
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_vacaciones_detalle(company, person):
    """Obtiene el detalle de vacaciones ejecutando sp_pr_vacacionesperson_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_vacacionesperson_web @cia=?, @person=?", (company, person))
        
        # Obtener nombres de columnas
        columns = [column[0] for column in cursor.description]
        # Convertir a lista de diccionarios
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_vacaciones_detalle: {e}")
        return []


def get_ausencias_detalle(company, person):
    """Obtiene el detalle de ausencias ejecutando sp_pr_ausenciasperson_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_ausenciasperson_web @cia=?, @person=?", (company, person))
        
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_ausencias_detalle: {e}")
        return []


def _fecha_a_date(val):
    """Convierte valor de BD a date."""
    if val is None:
        return None
    if hasattr(val, 'date') and callable(getattr(val, 'date')):
        return val.date()
    if hasattr(val, 'isoformat'):
        from datetime import date as date_type
        return date_type.fromisoformat(str(val).split(' ')[0])
    return None


# Paleta de colores distintos por motivo de ausencia (evitar verde vacaciones y naranja feriado)
PALETA_AUSENCIAS = [
    '#722f37',  # marrón
    '#0d9488',  # teal
    '#1e40af',  # azul
    '#b45309',  # ámbar
    '#6b21a8',  # púrpura
    '#be185d',  # rosa
    '#0369a1',  # sky
    '#0f766e',  # teal oscuro
    '#4f46e5',  # índigo
    '#9d174d',  # rosa oscuro
    '#7c2d12',  # marrón oscuro
    '#6366f1',  # violeta
]


def _expandir_rango_a_dias(start_date, end_date):
    """Genera (start, end) por cada día del rango [start_date, end_date] inclusive. end en formato exclusivo (día siguiente)."""
    from datetime import timedelta
    if start_date is None or end_date is None:
        return []
    if start_date > end_date:
        return []
    out = []
    d = start_date
    while d <= end_date:
        start_str = d.isoformat()
        next_d = d + timedelta(days=1)
        end_str = next_d.isoformat()
        out.append((start_str, end_str))
        d = next_d
    return out


def get_eventos_calendario(company, person_id):
    """Obtiene vacaciones y ausencias para mostrar en el calendario. Expande rangos a un evento por día para que se vea cada día en la vista anual."""
    try:
        from datetime import timedelta
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        eventos = []

        # Ausencias (sp_pr_ausenciasperson_web: MotivoAusencia, FechaInicio, FechaFin, Dias, Tipo, Solicitud) — un evento por día, color único por motivo
        try:
            cursor.execute("EXEC sp_pr_ausenciasperson_web @cia=?, @person=?", (company, person_id))
            if cursor.description:
                columns = [c[0] for c in cursor.description]
                rows_ausencia = []
                for row in cursor.fetchall():
                    d = dict(zip(columns, row))
                    motivo = (d.get('MotivoAusencia') or d.get('title') or 'Ausencia').strip()
                    start = d.get('FechaInicio') or d.get('start')
                    end = d.get('FechaFin') or d.get('end')
                    if start and end:
                        rows_ausencia.append((motivo, start, end))
                motivos_unicos = sorted(set(m[0] for m in rows_ausencia))
                color_por_motivo = {m: PALETA_AUSENCIAS[i % len(PALETA_AUSENCIAS)] for i, m in enumerate(motivos_unicos)}
                for motivo, start, end in rows_ausencia:
                    start_d = _fecha_a_date(start)
                    end_d = _fecha_a_date(end)
                    color = color_por_motivo.get(motivo, PALETA_AUSENCIAS[0])
                    for start_str, end_str in _expandir_rango_a_dias(start_d, end_d):
                        eventos.append({
                            'title': motivo,
                            'start': start_str,
                            'end': end_str,
                            'tipo': 'ausencia',
                            'backgroundColor': color,
                            'borderColor': color,
                            'extendedProps': {'tipo': 'ausencia', 'motivo': motivo}
                        })
        except Exception as ex:
            print(f"Error obteniendo ausencias para calendario: {ex}")

        # Vacaciones (SP sp_pr_vacacionesperson_web: FechaInicio, FechaFin, Dias, anio, Solicitud) — un evento por día
        try:
            cursor.execute("EXEC sp_pr_vacacionesperson_web @cia=?, @person=?", (company, person_id))
            if cursor.description:
                columns = [c[0] for c in cursor.description]
                for row in cursor.fetchall():
                    d = dict(zip(columns, row))
                    start = d.get('FechaInicio') or d.get('start')
                    end = d.get('FechaFin') or d.get('end')
                    if start and end:
                        start_d = _fecha_a_date(start)
                        end_d = _fecha_a_date(end)
                        for start_str, end_str in _expandir_rango_a_dias(start_d, end_d):
                            eventos.append({'title': 'VAC', 'start': start_str, 'end': end_str, 'tipo': 'vacacion', 'extendedProps': {'tipo': 'vacacion'}})
        except Exception as ex:
            print(f"Error obteniendo vacaciones para calendario: {ex}")

        # Formatear fechas para JSON y asignar colores (ausencias ya tienen color por motivo)
        for ev in eventos:
            ev['start'] = ev['start'].isoformat() if hasattr(ev['start'], 'isoformat') else ev['start']
            ev['end'] = ev['end'].isoformat() if hasattr(ev['end'], 'isoformat') else ev['end']
            if ev.get('tipo') == 'vacacion':
                ev['backgroundColor'] = '#10b981'
                ev['borderColor'] = '#059669'
            elif ev.get('tipo') != 'ausencia':
                ev['backgroundColor'] = '#722f37'
                ev['borderColor'] = ev['backgroundColor']

        cursor.close()
        conn.close()
        return eventos
    except Exception as e:
        print(f"Error en get_eventos_calendario: {e}")
        return []


def get_feriados():
    """Obtiene los feriados desde SY_Holiday para mostrarlos en el calendario."""
    from datetime import date, timedelta
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT HolidayDate as fecha, Description as motivo FROM SY_Holiday")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        feriados = []
        for row in rows:
            fecha = row[0]
            motivo = (row[1] or 'Feriado').strip()
            if not fecha:
                continue
            try:
                d = fecha.date() if hasattr(fecha, 'date') and callable(getattr(fecha, 'date')) else fecha
            except (AttributeError, TypeError):
                d = fecha
            start_str = d.isoformat() if hasattr(d, 'isoformat') else str(d).split(' ')[0]
            try:
                end_d = d + timedelta(days=1)
                end_str = end_d.isoformat()
            except (TypeError, AttributeError):
                end_str = start_str
            feriados.append({
                'title': motivo,
                'start': start_str,
                'end': end_str,
                'backgroundColor': '#f59e0b',
                'borderColor': '#d97706',
                'extendedProps': { 'tipo': 'feriado' }
            })
        return feriados
    except Exception as e:
        print(f"Error en get_feriados: {e}")
        return []


def get_tipos_documentos():
    """Obtiene la lista de tipos de documentos desde PR_tipodocWeb"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT Tipodocumento, name FROM PR_tipodocWeb")
        
        results = [{'Tipodocumento': row[0], 'name': row[1]} for row in cursor.fetchall()]
        
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_tipos_documentos: {e}")
        return []


def get_filtro_periodos(company):
    """Obtiene la lista de períodos disponibles ejecutando sp_pr_FiltroPeriodos_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_FiltroPeriodos_web @cia=?", (company,))
        
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_filtro_periodos: {e}")
        return []


def get_documentos_personales(company, person, tipodoc='BOL'):
    """Obtiene la lista de documentos disponibles (boletas) para el empleado.
    
    Args:
        company: ID de la compañía
        person: ID de la persona
        tipodoc: Tipo de documento (por defecto 'BOL')
    """
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("EXEC sp_pr_listadocumentos_web @cia=?, @person=?, @tipodoc=?", 
                      (company, person, tipodoc))
        
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_documentos_personales: {e}")
        return []


def actualizar_descarga(company, person, tipodocumento, prperiod):
    """Actualiza la fecha de descarga del documento ejecutando sp_pr_Actualizardescarga_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_Actualizardescarga_web @cia=?, @person=?, @tipodocumento=?, @prperiod=?", 
                      (company, person, tipodocumento, prperiod))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error en actualizar_descarga: {e}")
        return False


def actualizar_fechadescarga_boleta(company, person, tipodocumento, period):
    """
    Actualiza FechaDescarga en DocumentosBoletas para el documento descargado.
    """
    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE DocumentosBoletas
            SET FechaDescarga = GETDATE()
            WHERE Company = ?
              AND DNI = ?
              AND TipoDocumento = ?
              AND LEFT(Periodo, 6) = LEFT(?, 6)
            """,
            (company, person, tipodocumento, period),
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        print(f"Error en actualizar_fechadescarga_boleta: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def registrar_comprobante_web(company, payrolltype, processtype, period, person, userid, filename, tipo='BOL'):
    """Registra el comprobante generado en PR_DocumentPerson. SP sp_pr_registrarcomprobantes_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_registrarcomprobantes_web @cia=?, @payrolltype=?, @processtype=?, @period=?, @person=?, @userid=?, @filename=?, @tipo=?",
            (company, payrolltype, processtype, period, person, userid, filename, tipo)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error en registrar_comprobante_web: {e}")
        return False


def get_envio_comprobantes(company, tipodoc='BOL', prperiod=None):
    """Obtiene la lista de envío de comprobantes ejecutando sp_pr_enviocomprobantes_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        
        # El SP requiere el parámetro @prperiod
        # Si no se proporciona, se pasa None (el SP debería manejarlo o necesitará modificación)
        prperiod_value = prperiod if prperiod and prperiod.strip() else None
        
        cursor.execute("EXEC sp_pr_enviocomprobantes_web @cia=?, @tipodoc=?, @prperiod=?", 
                      (company, tipodoc, prperiod_value))
        
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_envio_comprobantes: {e}")
        return []


def get_reporte_descargas(company, tipodoc='BOL', prperiod=None):
    """Obtiene el reporte de descargas ejecutando sp_pr_reportedescargas_web.
    Devuelve: DNI, Nombre, Correo, NombreArchivo, FechaGenera, Primeradescarga.
    """
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        prperiod_value = prperiod if prperiod and prperiod.strip() else None
        cursor.execute(
            "EXEC sp_pr_reportedescargas_web @cia=?, @tipodoc=?, @prperiod=?",
            (company, tipodoc, prperiod_value)
        )
        columns = [column[0] for column in cursor.description]
        results = []
        for row in cursor.fetchall():
            d = dict(zip(columns, row))
            # Asegurar que 'Nombre' exista para la vista (el SP devuelve SY_Person.Name as Nombre)
            if 'Nombre' not in d or d.get('Nombre') is None or str(d.get('Nombre', '')).strip() == '':
                d['Nombre'] = d.get('Name') or d.get('nombre') or ''
            results.append(d)
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_reporte_descargas: {e}")
        return []


def actualizar_fecha_envio_db(company, person, tipodoc):
    """Actualiza la fecha de envío en PR_DocumentPerson"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        # Usamos GETDATE() para registrar el momento exacto del envío
        query = "UPDATE PR_DocumentPerson SET fechaenvio = GETDATE() WHERE Company = ? AND Person = ? AND Tipodocumento = ?"
        cursor.execute(query, (company, person, tipodoc))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error actualizando fecha de envío: {e}")
        return False


def registrar_solicitud_permiso(company, person, userid, controlyear, fechaini, fechafin, comentario):
    """Registra una solicitud de permiso ejecutando sp_pr_RegistrarSolicitudPermiso_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_RegistrarSolicitudPermiso_web @cia=?, @person=?, @userid=?, @controlyear=?, @fechaini=?, @fechaFin=?, @comentario=?", 
                      (company, person, userid, controlyear, fechaini, fechafin, comentario))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error en registrar_solicitud_permiso: {e}")
        return False


def get_max_dias_vacaciones(company):
    """Obtiene el máximo de días de vacaciones desde PR_mapping2 para la company (@cia)."""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT ISNULL(DiasVacaciones,0) FROM PR_mapping2 WHERE company = ?", (company,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row is not None and row[0] is not None:
            return int(row[0])
        return 30
    except Exception as e:
        print(f"Error en get_max_dias_vacaciones: {e}")
        return 30


def get_historial_solicitud_vacaciones(company, person, control_year=None):
    """Lista solicitudes de vacaciones de PR_SolicitudVacaciones por trabajador."""
    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        sql = """
            SELECT
                Id, Person, Company, DateBegin, DateEnd, Days, status,
                xlastuser, xlastdate, registerdate, ApprovalDate, ApprovalUser,
                Comments, ControlYear
            FROM PR_SolicitudVacaciones
            WHERE Company = ? AND Person = ?
        """
        params = [company, person]
        if control_year:
            sql += " AND ControlYear = ?"
            params.append(str(control_year).strip())
        sql += " ORDER BY registerdate DESC, Id DESC"
        cursor.execute(sql, tuple(params))
        cols = [column[0] for column in cursor.description]
        results = [dict(zip(cols, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_historial_solicitud_vacaciones: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def solicitud_vacaciones_tiene_cruce(company, person, date_begin, date_end, exclude_id=None):
    """
    True si el rango [date_begin, date_end] se solapa con otra solicitud
    del mismo trabajador (pendiente o aprobada).
    """
    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        sql = """
            SELECT TOP 1 1
            FROM PR_SolicitudVacaciones
            WHERE Company = ?
              AND Person = ?
              AND status IN ('P', 'A')
              AND DateBegin IS NOT NULL
              AND DateEnd IS NOT NULL
              AND CONVERT(date, DateBegin) <= CONVERT(date, ?)
              AND CONVERT(date, DateEnd) >= CONVERT(date, ?)
        """
        params = [
            str(company or '').strip(),
            str(person or '').strip(),
            str(date_end or '').strip(),
            str(date_begin or '').strip(),
        ]
        if exclude_id is not None:
            sql += " AND Id <> ?"
            params.append(int(exclude_id))
        cursor.execute(sql, tuple(params))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row is not None
    except Exception as e:
        print(f"Error en solicitud_vacaciones_tiene_cruce: {e}")
        return True
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_rangos_solicitud_vacaciones(company, person):
    """Rangos de fechas existentes (ISO) del trabajador para validación en cliente."""
    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                CONVERT(varchar(10), DateBegin, 23),
                CONVERT(varchar(10), DateEnd, 23)
            FROM PR_SolicitudVacaciones
            WHERE Company = ?
              AND Person = ?
              AND status IN ('P', 'A')
              AND DateBegin IS NOT NULL
              AND DateEnd IS NOT NULL
            ORDER BY DateBegin
            """,
            (str(company or '').strip(), str(person or '').strip()),
        )
        rangos = []
        for row in cursor.fetchall():
            ini = str(row[0] or '').strip()
            fin = str(row[1] or '').strip()
            if ini and fin:
                rangos.append({'begin': ini, 'end': fin})
        cursor.close()
        conn.close()
        return rangos
    except Exception as e:
        print(f"Error en get_rangos_solicitud_vacaciones: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def registrar_solicitud_vacaciones(company, person, date_begin, date_end, days, comments, control_year, user_id):
    """Inserta una solicitud en PR_SolicitudVacaciones con estado Pendiente (P)."""
    if solicitud_vacaciones_tiene_cruce(company, person, date_begin, date_end):
        return False
    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO PR_SolicitudVacaciones (
                Person, Company, DateBegin, DateEnd, Days, status,
                xlastuser, xlastdate, registerdate, Comments, ControlYear
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CONVERT(varchar(19), GETDATE(), 120), GETDATE(), ?, ?)
            """,
            (
                str(person or '').strip(),
                str(company or '').strip(),
                date_begin,
                date_end,
                int(days),
                'P',
                str(user_id or '').strip()[:20],
                (comments or '').strip()[:255],
                str(control_year or '').strip()[:4],
            ),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error en registrar_solicitud_vacaciones: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def eliminar_solicitud_vacaciones(company, person, solicitud_id):
    """Elimina una solicitud pendiente del trabajador (solo status P)."""
    conn = None
    try:
        sid = int(solicitud_id)
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM PR_SolicitudVacaciones
            WHERE Id = ?
              AND Company = ?
              AND Person = ?
              AND status = 'P'
            """,
            (sid, str(company or '').strip(), str(person or '').strip()),
        )
        deleted = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        conn.close()
        return deleted
    except Exception as e:
        print(f"Error en eliminar_solicitud_vacaciones: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def extraer_drive_file_id_sustento_vacaciones(comments):
    """Extrae el file_id de Drive guardado en Comments (prefijo SUSTENTO_DRIVE:)."""
    raw = str(comments or '').strip()
    prefix = 'SUSTENTO_DRIVE:'
    if raw.upper().startswith(prefix):
        return raw[len(prefix):].strip()
    return ''


def obtener_sustento_drive_ids_por_solicitudes(company, solicitud_ids):
    """
    Mapa { solicitud_id: drive_file_id } para solicitudes aprobadas con sustento en Drive.
    """
    ids = []
    for sid in solicitud_ids or []:
        try:
            ids.append(int(sid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}

    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(ids))
        cursor.execute(
            f"""
            SELECT Id, Comments
            FROM PR_SolicitudVacaciones
            WHERE Company = ?
              AND status = 'A'
              AND Id IN ({placeholders})
            """,
            [str(company or '').strip(), *ids],
        )
        out = {}
        for row in cursor.fetchall():
            sid = row[0]
            fid = extraer_drive_file_id_sustento_vacaciones(row[1] if len(row) > 1 else '')
            if fid:
                out[int(sid)] = fid
        cursor.close()
        conn.close()
        return out
    except Exception as e:
        print(f'Error en obtener_sustento_drive_ids_por_solicitudes: {e}')
        return {}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def obtener_drive_file_id_sustento_vacaciones(solicitud_id, company):
    """file_id de Drive para una solicitud aprobada con sustento, o cadena vacía."""
    try:
        sid = int(solicitud_id)
    except (TypeError, ValueError):
        return ''
    m = obtener_sustento_drive_ids_por_solicitudes(company, [sid])
    return str(m.get(sid) or '').strip()


def aprobar_solicitud_vacaciones_con_sustento(solicitud_id, company, approval_user, drive_file_id):
    """
    Aprueba solicitud pendiente y registra el file_id de Drive en Comments (SUSTENTO_DRIVE:...).
    Retorna True si actualizó una fila.
    """
    conn = None
    try:
        sid = int(solicitud_id)
        drive_id = str(drive_file_id or '').strip()
        if not drive_id:
            return False
        comments = f'SUSTENTO_DRIVE:{drive_id}'
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE PR_SolicitudVacaciones
            SET status = 'A',
                ApprovalUser = ?,
                ApprovalDate = GETDATE(),
                Comments = ?,
                xlastuser = ?,
                xlastdate = CONVERT(varchar(19), GETDATE(), 120)
            WHERE Id = ?
              AND Company = ?
              AND status = 'P'
            """,
            (
                str(approval_user or '').strip()[:20],
                comments[:255],
                str(approval_user or '').strip()[:20],
                sid,
                str(company or '').strip(),
            ),
        )
        ok = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        conn.close()
        return ok
    except Exception as e:
        print(f'Error en aprobar_solicitud_vacaciones_con_sustento: {e}')
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_resumen_solicitud_vacaciones(company, person, control_year, dias_totales=30):
    """Calcula días consumidos/solicitados (A+P) y disponibles del ejercicio."""
    conn = None
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ISNULL(SUM(ISNULL(Days, 0)), 0)
            FROM PR_SolicitudVacaciones
            WHERE Company = ?
              AND Person = ?
              AND ControlYear = ?
              AND status IN ('P', 'A')
            """,
            (company, person, str(control_year).strip()),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        usados = int(row[0] or 0) if row else 0
        total = int(dias_totales or 30)
        disponibles = max(total - usados, 0)
        return {"total": total, "consumidos": usados, "disponibles": disponibles}
    except Exception as e:
        print(f"Error en get_resumen_solicitud_vacaciones: {e}")
        total = int(dias_totales or 30)
        return {"total": total, "consumidos": 0, "disponibles": total}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_constancia_datos(company, person):
    """
    Obtiene los datos para la constancia de trabajo ejecutando sp_pr_constanciatrabajo_web
    """
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_constanciatrabajo_web @person=?, @cia=?", (person, company))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return None
        columns = [column[0] for column in cursor.description]
        result = dict(zip(columns, row))
        cursor.close()
        conn.close()
        return result
    except Exception as e:
        print(f"Error en get_constancia_datos: {e}")
        return None


def get_lista_solicitudes_permiso(company, person):
    """Obtiene la lista de solicitudes de permiso ejecutando sp_pr_ListarSolicitudPermiso_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_ListarSolicitudPermiso_web @cia=?, @person=?", (company, person))
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_lista_solicitudes_permiso: {e}")
        return []


def get_aprobacion_solicitudes_pendientes(company, name=None, estado='P'):
    """Obtiene la lista de solicitudes ejecutando sp_pr_AprobarSolicitudesPendientes_web.
    name: filtro opcional por nombre. estado: 'P' Pendiente, 'A' Aprobado, 'T' Todos. Por defecto P."""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        name_param = (name or '').strip()
        estado_param = (estado or 'P').strip().upper()
        if estado_param not in ('P', 'A', 'T'):
            estado_param = 'P'
        cursor.execute(
            "EXEC sp_pr_AprobarSolicitudesPendientes_web @cia=?, @name=?, @estado=?",
            (company, name_param, estado_param)
        )
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error en get_aprobacion_solicitudes_pendientes: {e}")
        return []


def eliminar_solicitud_permiso(company, person, line):
    """Elimina una solicitud de permiso ejecutando sp_pr_EliminarSolicitudPermiso_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_EliminarSolicitudPermiso_web @cia=?, @person=?, @line=?",
            (company, person, int(line))
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error en eliminar_solicitud_permiso: {e}")
        return False


def aprobar_solicitud_web(company, person, controlyear, line, userid):
    """Aprueba una solicitud ejecutando sp_pr_AprobarSolicitud_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_AprobarSolicitud_web @cia=?, @person=?, @controlyear=?, @line=?, @userid=?",
            (company, person, str(controlyear), int(line), userid)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error en aprobar_solicitud_web: {e}")
        return False


def get_boleta_cabecera(company, process, payrolltype, period, person):
    """Ejecuta sp_pr_generarboleta_web para datos del encabezado"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_generarboleta_web @cia=?, @process=?, @payrolltype=?, @period=?, @person=?",
            (company, process, payrolltype, period, person)
        )
        columns = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return dict(zip(columns, row)) if row else None
    except Exception as e:
        print(f"Error get_boleta_cabecera: {e}")
        return None


def get_boleta_conceptos(company, process, payrolltype, period, person, tipo):
    """
    Ejecuta los SPs de detalle según el tipo:
    tipo='I': Ingresos, tipo='D': Descuentos, tipo='A': Aportes
    """
    sp_map = {
        'I': 'sp_pr_detalleboletaingresos_web',
        'D': 'sp_pr_detalleboletadescuentos_web',
        'A': 'sp_pr_detalleboletaaportes_web'
    }
    sp_name = sp_map.get(tipo)
    if not sp_name:
        return []
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"EXEC {sp_name} @cia=?, @process=?, @payrolltype=?, @period=?, @person=?",
            (company, process, payrolltype, period, person)
        )
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error get_boleta_conceptos ({tipo}): {e}")
        return []


def get_selector_planillas(company):
    """Obtiene tipos de planilla para el selector. SP sp_pr_selectorplanillas_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute("EXEC sp_pr_selectorplanillas_web @cia=?", (company,))
        if cursor.description is None:
            cursor.close()
            conn.close()
            return []
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error get_selector_planillas: {e}")
        return []


def get_selector_procesos(company, payrolltype):
    """Obtiene procesos para el selector. SP sp_pr_selectorprocesos_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_selectorprocesos_web @cia=?, @payrolltype=?",
            (company, payrolltype)
        )
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error get_selector_procesos: {e}")
        return []


def get_selector_periodos(company, payrolltype, processtype):
    """Obtiene periodos para el selector. SP sp_pr_selectorperiodos_web"""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_pr_selectorperiodos_web @cia=?, @payrolltype=?, @processtype=?",
            (company, payrolltype, processtype)
        )
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error get_selector_periodos: {e}")
        return []


def get_listado_generar_boletas(company, payrolltype, processtype, period, person=None):
    """Obtiene listado para generar boletas. SP sp_pr_listadogenerarboletas_web (@person opcional)."""
    try:
        conn = DatabaseConfig.get_connection()
        cursor = conn.cursor()
        person_val = (person or '').strip() if person is not None else ''
        if not person_val:
            person_val = '0'
        cursor.execute(
            "EXEC sp_pr_listadogenerarboletas_web @cia=?, @payrolltype=?, @processtype=?, @period=?, @person=?",
            (company, payrolltype, processtype, period, person_val)
        )
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error get_listado_generar_boletas: {e}")
        return []


def get_inventario_categorias():
    """Listado de categorías para el formulario de artículos."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdCategoria, NombreCategoria
            FROM dbo.Inventario_Categorias
            ORDER BY NombreCategoria
            """
        )
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_inventario_categorias: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_inventario_marcas():
    """Listado de marcas para el formulario de artículos."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdMarca, NombreMarca
            FROM dbo.Inventario_Marcas
            ORDER BY NombreMarca
            """
        )
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_inventario_marcas: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def insertar_inventario_item(
    codigo,
    id_categoria,
    id_marca,
    descripcion,
    aplicacion=None,
    codigo_bomba=None,
    stock_inicial=0,
    xlastuser=None,
):
    """
    Registra un artículo en Inventario_Items.
    Returns:
        (True, mensaje) o (False, mensaje_error)
    """
    conn = None
    codigo = (codigo or "").strip()
    descripcion = (descripcion or "").strip()
    if not codigo:
        return False, "El código es obligatorio."
    if not descripcion:
        return False, "La descripción es obligatoria."
    if not id_categoria or not id_marca:
        return False, "Seleccione categoría y marca."

    try:
        stock = int(stock_inicial or 0)
        if stock < 0:
            return False, "El stock inicial no puede ser negativo."
    except (TypeError, ValueError):
        return False, "Stock inicial no válido."

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.Inventario_Items (
                Codigo, IdCategoria, IdMarca, Descripcion,
                Aplicacion, CodigoBomba, StockActual,
                xlastuser, xlastdate
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, GETDATE())
            """,
            (
                codigo,
                int(id_categoria),
                int(id_marca),
                descripcion,
                (aplicacion or "").strip() or None,
                (codigo_bomba or "").strip() or None,
                stock,
                (xlastuser or "").strip()[:20] or None,
            ),
        )
        conn.commit()
        cursor.close()
        return True, f"Artículo «{codigo}» registrado correctamente."
    except pyodbc.IntegrityError as e:
        err = str(e).lower()
        if "uq_codigoitem" in err or "unique" in err:
            return False, "Ya existe un artículo con ese código."
        if "fk_items_categorias" in err:
            return False, "La categoría seleccionada no es válida."
        if "fk_items_marcas" in err:
            return False, "La marca seleccionada no es válida."
        return False, "No se pudo guardar: datos duplicados o referencia inválida."
    except Exception as e:
        _logger_db.exception("insertar_inventario_item: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al guardar el artículo. Verifique la conexión a la base de datos."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_inventario_item_por_id(iditem):
    """Obtiene un artículo por IdItem para edición."""
    conn = None
    try:
        iditem = int(iditem)
    except (TypeError, ValueError):
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdItem, Codigo, IdCategoria, IdMarca, Descripcion,
                   Aplicacion, CodigoBomba, StockActual
            FROM dbo.Inventario_Items
            WHERE IdItem = ?
            """,
            (iditem,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return None
        columns = [col[0] for col in cursor.description]
        item = {col: val for col, val in zip(columns, row)}
        cursor.close()
        return {k.lower(): v for k, v in item.items()}
    except Exception as e:
        _logger_db.exception("get_inventario_item_por_id: %s", e)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def actualizar_inventario_item(
    iditem,
    id_categoria,
    id_marca,
    descripcion,
    aplicacion=None,
    codigo_bomba=None,
    stock_actual=0,
    xlastuser=None,
):
    """Actualiza un artículo (el código no se modifica)."""
    conn = None
    try:
        iditem = int(iditem)
    except (TypeError, ValueError):
        return False, "Artículo no válido."
    descripcion = (descripcion or "").strip()
    if not descripcion:
        return False, "La descripción es obligatoria."
    if not id_categoria or not id_marca:
        return False, "Seleccione categoría y marca."
    try:
        stock = int(stock_actual or 0)
        if stock < 0:
            return False, "El stock no puede ser negativo."
    except (TypeError, ValueError):
        return False, "Stock no válido."

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE dbo.Inventario_Items
            SET IdCategoria = ?, IdMarca = ?, Descripcion = ?,
                Aplicacion = ?, CodigoBomba = ?, StockActual = ?,
                xlastuser = ?, xlastdate = GETDATE()
            WHERE IdItem = ?
            """,
            (
                int(id_categoria),
                int(id_marca),
                descripcion,
                (aplicacion or "").strip() or None,
                (codigo_bomba or "").strip() or None,
                stock,
                (xlastuser or "").strip()[:20] or None,
                iditem,
            ),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            cursor.close()
            return False, "No se encontró el artículo a actualizar."
        conn.commit()
        cursor.close()
        return True, "Artículo actualizado correctamente."
    except pyodbc.IntegrityError as e:
        err = str(e).lower()
        if "fk_items_categorias" in err:
            return False, "La categoría seleccionada no es válida."
        if "fk_items_marcas" in err:
            return False, "La marca seleccionada no es válida."
        return False, "No se pudo actualizar: referencia inválida."
    except Exception as e:
        _logger_db.exception("actualizar_inventario_item: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al actualizar el artículo."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def eliminar_inventario_item(iditem):
    """Elimina un artículo por IdItem."""
    conn = None
    try:
        iditem = int(iditem)
    except (TypeError, ValueError):
        return False, "Artículo no válido."
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        listas = []
        cursor.execute(
            """
            SELECT TOP 1 1
            FROM dbo.Inventario_ComprasDet d
            INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
            WHERE d.IdItem = ?
              AND UPPER(LTRIM(RTRIM(ISNULL(c.EstadoCompra, '')))) <> 'ANULADA'
            """,
            (iditem,),
        )
        if cursor.fetchone():
            listas.append("lista de compras")
        cursor.execute(
            """
            SELECT TOP 1 1
            FROM dbo.Inventario_VentasDet d
            INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
            WHERE d.IdItem = ?
              AND UPPER(LTRIM(RTRIM(ISNULL(v.EstadoVenta, '')))) <> 'ANULADA'
            """,
            (iditem,),
        )
        if cursor.fetchone():
            listas.append("lista de ventas")
        cursor.execute(
            "SELECT TOP 1 1 FROM dbo.Inventario_ProformasDet WHERE IdItem = ?",
            (iditem,),
        )
        if cursor.fetchone():
            listas.append("lista de proformas")

        if listas:
            cursor.close()
            if len(listas) == 1:
                msg = f"No se puede eliminar el artículo porque está registrado en la {listas[0]}."
            elif len(listas) == 2:
                msg = (
                    f"No se puede eliminar el artículo porque está registrado en la "
                    f"{listas[0]} y en la {listas[1]}."
                )
            else:
                msg = (
                    "No se puede eliminar el artículo porque está registrado en la "
                    f"{listas[0]}, en la {listas[1]} y en la {listas[2]}."
                )
            return False, msg

        # Las compras/ventas anuladas conservan detalle histórico; hay que quitar esas
        # líneas antes de borrar el artículo para no violar la FK.
        cursor.execute(
            """
            DELETE d
            FROM dbo.Inventario_ComprasDet d
            INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
            WHERE d.IdItem = ?
              AND UPPER(LTRIM(RTRIM(ISNULL(c.EstadoCompra, '')))) = 'ANULADA'
            """,
            (iditem,),
        )
        cursor.execute(
            """
            DELETE d
            FROM dbo.Inventario_VentasDet d
            INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
            WHERE d.IdItem = ?
              AND UPPER(LTRIM(RTRIM(ISNULL(v.EstadoVenta, '')))) = 'ANULADA'
            """,
            (iditem,),
        )

        cursor.execute(
            "DELETE FROM dbo.Inventario_Items WHERE IdItem = ?",
            (iditem,),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            cursor.close()
            return False, "No se encontró el artículo a eliminar."
        conn.commit()
        cursor.close()
        return True, "Artículo eliminado correctamente."
    except pyodbc.IntegrityError as e:
        _logger_db.exception("eliminar_inventario_item: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        err = str(e).lower()
        if "fk_ventasdet_items" in err or "inventario_ventasdet" in err:
            return False, "No se puede eliminar el artículo porque está registrado en la lista de ventas."
        if "fk_comprasdet_items" in err or "inventario_comprasdet" in err:
            return False, "No se puede eliminar el artículo porque está registrado en la lista de compras."
        if "fk_proformasdet_items" in err or "inventario_proformasdet" in err:
            return False, "No se puede eliminar el artículo porque está registrado en la lista de proformas."
        return False, "No se puede eliminar el artículo porque está referenciado en otros registros."
    except Exception as e:
        _logger_db.exception("eliminar_inventario_item: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al eliminar el artículo."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _rows_to_dicts(cursor):
    columns = [col[0] for col in cursor.description]
    rows = []
    for row in cursor.fetchall():
        item = {col: val for col, val in zip(columns, row)}
        rows.append({k.lower(): v for k, v in item.items()})
    return rows


def get_historial_movimientos_item(iditem):
    """Kárdex del artículo: compras y ventas no anuladas con totales de cantidad."""
    conn = None
    try:
        iditem = int(iditem)
    except (TypeError, ValueError):
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1 viaje: artículo + totales calculados en SQL (filtro sargable, sin UPPER/LTRIM)
        cursor.execute(
            """
            SELECT
                i.IdItem,
                i.Codigo,
                i.Descripcion,
                i.StockActual,
                ISNULL(comp.TotalCantidad, 0) AS TotalCompras,
                ISNULL(vent.TotalCantidad, 0) AS TotalVentas
            FROM dbo.Inventario_Items i
            OUTER APPLY (
                SELECT SUM(d.Cantidad) AS TotalCantidad
                FROM dbo.Inventario_ComprasDet d
                INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
                WHERE d.IdItem = i.IdItem
                  AND ISNULL(c.EstadoCompra, '') <> 'ANULADA'
            ) comp
            OUTER APPLY (
                SELECT SUM(d.Cantidad) AS TotalCantidad
                FROM dbo.Inventario_VentasDet d
                INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
                WHERE d.IdItem = i.IdItem
                  AND ISNULL(v.EstadoVenta, '') <> 'ANULADA'
            ) vent
            WHERE i.IdItem = ?
            """,
            (iditem,),
        )
        row_item = cursor.fetchone()
        if not row_item:
            cursor.close()
            return None

        item_cols = [col[0] for col in cursor.description]
        item_row = {k.lower(): v for k, v in zip(item_cols, row_item)}
        total_compras = int(item_row.pop('totalcompras', 0) or 0)
        total_ventas = int(item_row.pop('totalventas', 0) or 0)
        item = item_row
        stock_actual = int(item.get('stockactual') or 0)
        stock_calculado = total_compras - total_ventas

        # 1 viaje: detalle unificado (compras + ventas) ordenado cronológicamente
        cursor.execute(
            """
            SELECT
                mov.Tipo,
                mov.RazonSocial,
                mov.FechaMovimiento,
                mov.PrecioUnitario,
                mov.Cantidad,
                mov.TotalLinea
            FROM (
                SELECT
                    CAST('COMPRA' AS VARCHAR(10)) AS Tipo,
                    e.RazonSocial,
                    c.FechaCompra AS FechaMovimiento,
                    d.PrecioUnitario,
                    d.Cantidad,
                    d.TotalLinea,
                    d.IdCompraDet AS IdOrden
                FROM dbo.Inventario_ComprasDet d
                INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
                INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = c.IdProveedor
                WHERE d.IdItem = ?
                  AND ISNULL(c.EstadoCompra, '') <> 'ANULADA'

                UNION ALL

                SELECT
                    CAST('VENTA' AS VARCHAR(10)) AS Tipo,
                    e.RazonSocial,
                    v.FechaVenta AS FechaMovimiento,
                    d.PrecioUnitario,
                    d.Cantidad,
                    d.TotalLinea,
                    d.IdVentaDet AS IdOrden
                FROM dbo.Inventario_VentasDet d
                INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
                INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = v.IdCliente
                WHERE d.IdItem = ?
                  AND ISNULL(v.EstadoVenta, '') <> 'ANULADA'
            ) mov
            ORDER BY mov.FechaMovimiento, mov.Tipo, mov.IdOrden
            """,
            (iditem, iditem),
        )
        compras = []
        ventas = []
        for row in cursor.fetchall():
            mov = {
                'tipo': row[0],
                'razonsocial': row[1],
                'fechacompra': row[2] if row[0] == 'COMPRA' else None,
                'fechaventa': row[2] if row[0] == 'VENTA' else None,
                'preciounitario': row[3],
                'cantidad': row[4],
                'totallinea': row[5],
            }
            if mov['tipo'] == 'COMPRA':
                compras.append(mov)
            else:
                ventas.append(mov)
        cursor.close()

        return {
            'item': item,
            'compras': compras,
            'ventas': ventas,
            'totales': {
                'cantidad_compras': total_compras,
                'cantidad_ventas': total_ventas,
                'stock_calculado': stock_calculado,
                'stock_actual': stock_actual,
                'cuadra': stock_calculado == stock_actual,
            },
        }
    except Exception as e:
        _logger_db.exception("get_historial_movimientos_item: %s", e)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_listado_articulos_inventario(codigo='', nombre=''):
    """Ejecuta sp_listadoarticulos_inventario."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_listadoarticulos_inventario @codigo=?, @nombre=?",
            ((codigo or '').strip(), (nombre or '').strip()),
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_listadoarticulos_inventario: %s", e)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_ubigeo_departamentos():
    """Departamentos para selector de ubigeo."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdDepartamento, NombreDepartamento
            FROM dbo.Ubigeo_Departamentos
            ORDER BY NombreDepartamento
            """
        )
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_ubigeo_departamentos: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_ubigeo_provincias(id_departamento):
    """Provincias filtradas por departamento."""
    id_departamento = (id_departamento or "").strip()
    if not id_departamento:
        return []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdProvincia, NombreProvincia
            FROM dbo.Ubigeo_Provincias
            WHERE IdDepartamento = ?
            ORDER BY NombreProvincia
            """,
            (id_departamento,),
        )
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_ubigeo_provincias: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_ubigeo_distritos(id_provincia):
    """Distritos filtrados por provincia."""
    id_provincia = (id_provincia or "").strip()
    if not id_provincia:
        return []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdDistrito, NombreDistrito
            FROM dbo.Ubigeo_Distritos
            WHERE IdProvincia = ?
            ORDER BY NombreDistrito
            """,
            (id_provincia,),
        )
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_ubigeo_distritos: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _empresa_flags_desde_form(es_cliente, es_proveedor):
    """Convierte valores de formulario a bit (0/1)."""
    def _bit(val):
        if val is None:
            return 0
        s = str(val).strip().lower()
        if s in ('1', 'true', 'on', 'yes', 'si', 'sí'):
            return 1
        try:
            return 1 if int(s) else 0
        except (TypeError, ValueError):
            return 0

    cli = _bit(es_cliente)
    prov = _bit(es_proveedor)
    return cli, prov


def _estado_bit_desde_form(estado):
    """Checkbox estado: ausente o desmarcado = inactivo (0)."""
    if estado is None:
        return 0
    s = str(estado).strip().lower()
    if s in ('1', 'true', 'on', 'yes'):
        return 1
    if s in ('0', 'false', 'off', 'no'):
        return 0
    try:
        return 1 if int(estado) else 0
    except (TypeError, ValueError):
        return 0


def insertar_inventario_empresa(
    ruc,
    razon_social,
    direccion=None,
    id_departamento=None,
    id_provincia=None,
    id_distrito=None,
    telefono=None,
    correo=None,
    es_cliente=0,
    es_proveedor=0,
    estado=1,
):
    """Registra proveedor/cliente en Inventario_Empresas."""
    conn = None
    ruc = (ruc or "").strip()
    razon_social = (razon_social or "").strip().upper()
    if not ruc:
        return False, "El RUC es obligatorio."
    if not razon_social:
        return False, "La razón social es obligatoria."
    es_cliente, es_proveedor = _empresa_flags_desde_form(es_cliente, es_proveedor)
    if not es_cliente and not es_proveedor:
        return False, "Indique si la empresa es cliente, proveedor o ambos."
    if not (id_distrito or "").strip():
        return False, "Seleccione el distrito (ubigeo)."

    estado_bit = _estado_bit_desde_form(estado)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.Inventario_Empresas (
                RUC, RazonSocial, Direccion,
                IdDepartamento, IdProvincia, IdDistrito,
                Telefono, Correo, EsCliente, EsProveedor, Estado
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ruc,
                razon_social,
                (direccion or "").strip() or None,
                (id_departamento or "").strip() or None,
                (id_provincia or "").strip() or None,
                (id_distrito or "").strip(),
                (telefono or "").strip() or None,
                (correo or "").strip() or None,
                es_cliente,
                es_proveedor,
                estado_bit,
            ),
        )
        conn.commit()
        cursor.close()
        return True, f"Empresa «{razon_social}» registrada correctamente."
    except pyodbc.IntegrityError as e:
        err = str(e).lower()
        if "uq_empresas_ruc" in err or "unique" in err:
            return False, "Ya existe una empresa con ese RUC."
        if "fk_empresas_distritos" in err:
            return False, "El distrito seleccionado no es válido."
        return False, "No se pudo guardar: datos duplicados o referencia inválida."
    except Exception as e:
        _logger_db.exception("insertar_inventario_empresa: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al guardar la empresa. Verifique la conexión a la base de datos."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_inventario_empresa_por_id(idempresa):
    """Obtiene una empresa por IdEmpresa para edición."""
    conn = None
    try:
        idempresa = int(idempresa)
    except (TypeError, ValueError):
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdEmpresa, RUC, RazonSocial, Direccion,
                   IdDepartamento, IdProvincia, IdDistrito,
                   Telefono, Correo, EsCliente, EsProveedor, Estado
            FROM dbo.Inventario_Empresas
            WHERE IdEmpresa = ?
            """,
            (idempresa,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return None
        columns = [col[0] for col in cursor.description]
        item = {col: val for col, val in zip(columns, row)}
        cursor.close()
        return {k.lower(): v for k, v in item.items()}
    except Exception as e:
        _logger_db.exception("get_inventario_empresa_por_id: %s", e)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def actualizar_inventario_empresa(
    idempresa,
    ruc,
    razon_social,
    direccion=None,
    id_departamento=None,
    id_provincia=None,
    id_distrito=None,
    telefono=None,
    correo=None,
    es_cliente=0,
    es_proveedor=0,
    estado=1,
):
    """Actualiza empresa en Inventario_Empresas."""
    conn = None
    try:
        idempresa = int(idempresa)
    except (TypeError, ValueError):
        return False, "Empresa no válida."
    ruc = (ruc or "").strip()
    if not ruc:
        return False, "El RUC es obligatorio."
    razon_social = (razon_social or "").strip().upper()
    if not razon_social:
        return False, "La razón social es obligatoria."
    es_cliente, es_proveedor = _empresa_flags_desde_form(es_cliente, es_proveedor)
    if not es_cliente and not es_proveedor:
        return False, "Indique si la empresa es cliente, proveedor o ambos."
    if not (id_distrito or "").strip():
        return False, "Seleccione el distrito (ubigeo)."

    estado_bit = _estado_bit_desde_form(estado)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE dbo.Inventario_Empresas
            SET RUC = ?, RazonSocial = ?, Direccion = ?,
                IdDepartamento = ?, IdProvincia = ?, IdDistrito = ?,
                Telefono = ?, Correo = ?, EsCliente = ?, EsProveedor = ?, Estado = ?
            WHERE IdEmpresa = ?
            """,
            (
                ruc,
                razon_social,
                (direccion or "").strip() or None,
                (id_departamento or "").strip() or None,
                (id_provincia or "").strip() or None,
                (id_distrito or "").strip(),
                (telefono or "").strip() or None,
                (correo or "").strip() or None,
                es_cliente,
                es_proveedor,
                estado_bit,
                idempresa,
            ),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            cursor.close()
            return False, "No se encontró la empresa a actualizar."
        conn.commit()
        cursor.close()
        return True, "Empresa actualizada correctamente."
    except pyodbc.IntegrityError as e:
        err = str(e).lower()
        if "uq_empresas_ruc" in err or "unique" in err:
            return False, "Ya existe otra empresa con ese RUC."
        if "fk_empresas_distritos" in err:
            return False, "El distrito seleccionado no es válido."
        return False, "No se pudo actualizar: referencia inválida."
    except Exception as e:
        _logger_db.exception("actualizar_inventario_empresa: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al actualizar la empresa."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def eliminar_inventario_empresa(idempresa):
    """Elimina una empresa por IdEmpresa."""
    conn = None
    try:
        idempresa = int(idempresa)
    except (TypeError, ValueError):
        return False, "Empresa no válida."
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM dbo.Inventario_Empresas WHERE IdEmpresa = ?",
            (idempresa,),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            cursor.close()
            return False, "No se encontró la empresa a eliminar."
        conn.commit()
        cursor.close()
        return True, "Empresa eliminada correctamente."
    except Exception as e:
        _logger_db.exception("eliminar_inventario_empresa: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al eliminar la empresa. Puede estar referenciada en otros registros."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_listado_empresas_inventario(ruc='', razon_social='', tipo='TODOS'):
    """Listado de empresas con filtros opcionales."""
    conn = None
    ruc_s = (ruc or '').strip()
    nombre_s = (razon_social or '').strip()
    ruc_like = f'%{ruc_s}%'
    nombre_like = f'%{nombre_s}%'
    tipo_filtro = (tipo or 'TODOS').strip().upper()
    if tipo_filtro not in ('TODOS', 'CLIENTE', 'PROVEEDOR'):
        tipo_filtro = 'TODOS'
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                e.IdEmpresa,
                e.RUC,
                e.RazonSocial,
                e.Telefono,
                e.Correo,
                e.EsCliente,
                e.EsProveedor,
                e.Estado,
                dep.NombreDepartamento,
                p.NombreProvincia,
                d.NombreDistrito
            FROM dbo.Inventario_Empresas e
            LEFT JOIN dbo.Ubigeo_Distritos d ON e.IdDistrito = d.IdDistrito
            LEFT JOIN dbo.Ubigeo_Provincias p ON e.IdProvincia = p.IdProvincia
            LEFT JOIN dbo.Ubigeo_Departamentos dep ON e.IdDepartamento = dep.IdDepartamento
            WHERE (? = '' OR e.RUC LIKE ?)
              AND (? = '' OR e.RazonSocial LIKE ?)
              AND (
                    ? = 'TODOS'
                    OR (? = 'CLIENTE' AND e.EsCliente = 1)
                    OR (? = 'PROVEEDOR' AND e.EsProveedor = 1)
                  )
            ORDER BY e.RazonSocial
            """,
            (ruc_s, ruc_like, nombre_s, nombre_like, tipo_filtro, tipo_filtro, tipo_filtro),
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_listado_empresas_inventario: %s", e)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_proveedores_activos():
    """Proveedores activos para selector de compras."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdEmpresa, RUC, RazonSocial
            FROM dbo.Inventario_Empresas
            WHERE EsProveedor = 1 AND Estado = 1
            ORDER BY RazonSocial
            """
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_proveedores_activos: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_articulos_para_compra():
    """Artículos para líneas de detalle en compras."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdItem, Codigo, Descripcion
            FROM dbo.Inventario_Items
            ORDER BY Descripcion, Codigo
            """
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_articulos_para_compra: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_lista_compras_inventario(codigo='', articulo='', proveedor=0, estado_pago=''):
    """Ejecuta sp_inv_lista_compras con filtros."""
    conn = None
    codigo_s = (codigo or '').strip()
    articulo_s = (articulo or '').strip()
    estado_pago_s = (estado_pago or '').strip().upper()
    if estado_pago_s and estado_pago_s not in ('PENDIENTE', 'CANCELADO'):
        estado_pago_s = ''
    try:
        proveedor_i = int(proveedor or 0)
    except (TypeError, ValueError):
        proveedor_i = 0
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_inv_lista_compras @codigo=?, @articulo=?, @proveedor=?, @estadopago=?",
            (codigo_s, articulo_s, proveedor_i, estado_pago_s),
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            item = {k.lower(): v for k, v in item.items()}
            estado = str(item.get('estadocompra') or '').strip().upper()
            if estado in ('ANULADA', 'ANULADO'):
                continue
            rows.append(item)
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_lista_compras_inventario: %s", e)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_compra_por_id(id_compra):
    """Obtiene cabecera y detalle de una compra para edición."""
    conn = None
    try:
        id_compra_i = int(id_compra)
    except (TypeError, ValueError):
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                c.IdCompra, c.IdProveedor, c.FechaCompra, c.TipoComprobante, c.NroComprobanteRef,
                c.IncluyeIGV, c.EstadoPago, c.EstadoCompra
            FROM dbo.Inventario_ComprasCab c
            WHERE c.IdCompra = ?
            """,
            (id_compra_i,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return None
        columns = [col[0] for col in cursor.description]
        cab = {k.lower(): v for k, v in dict(zip(columns, row)).items()}

        cursor.execute(
            """
            SELECT
                d.IdItem, i.Codigo, i.Descripcion, d.Cantidad, d.PrecioUnitario, d.TotalLinea
            FROM dbo.Inventario_ComprasDet d
            INNER JOIN dbo.Inventario_Items i ON d.IdItem = i.IdItem
            WHERE d.IdCompra = ?
            ORDER BY d.IdCompraDet
            """,
            (id_compra_i,),
        )
        det_cols = [col[0] for col in cursor.description]
        detalles = []
        for det in cursor.fetchall():
            item = {k.lower(): v for k, v in dict(zip(det_cols, det)).items()}
            detalles.append(item)
        cursor.close()
        cab['detalles'] = detalles
        return cab
    except Exception as e:
        _logger_db.exception("get_compra_por_id: %s", e)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _calcular_totales_compra(detalles, incluye_igv):
    """Calcula subtotal, IGV y total a partir de líneas de detalle."""
    from decimal import Decimal, ROUND_HALF_UP

    suma_lineas = Decimal('0')
    for d in detalles:
        cantidad = int(d['cantidad'])
        precio = Decimal(str(d['precio_unitario']))
        suma_lineas += Decimal(cantidad) * precio

    total = suma_lineas.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if incluye_igv:
        subtotal = (total / Decimal('1.18')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        igv = (total - subtotal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        subtotal = total
        igv = Decimal('0.00')
    return subtotal, igv, total


def insertar_compra(
    id_proveedor,
    fecha_compra,
    tipo_comprobante,
    nro_comprobante_ref,
    incluye_igv,
    estado_pago,
    detalles,
):
    """
    Registra cabecera y detalle de compra; incrementa stock por cada línea.
    detalles: lista de dicts con id_item, cantidad, precio_unitario.
    """
    from decimal import Decimal, ROUND_HALF_UP

    if not detalles:
        return False, "Debe agregar al menos un artículo al detalle."

    try:
        id_proveedor = int(id_proveedor)
    except (TypeError, ValueError):
        return False, "Proveedor no válido."

    tipos_ok = ('FACTURA', 'BOLETA', 'NOTA_VENTA', 'NINGUNO')
    tipo = (tipo_comprobante or '').strip().upper()
    if tipo not in tipos_ok:
        return False, "Tipo de comprobante no válido."

    estado_pago_val = (estado_pago or 'PENDIENTE').strip().upper()
    if estado_pago_val not in ('PENDIENTE', 'CANCELADO'):
        return False, "Estado de pago no válido."

    lineas = []
    for d in detalles:
        try:
            id_item = int(d.get('id_item') or d.get('idItem'))
            cantidad = int(d.get('cantidad'))
            precio_unitario = Decimal(str(d.get('precio_unitario') or d.get('precioUnitario')))
        except (TypeError, ValueError, ArithmeticError):
            return False, "Línea de detalle con datos inválidos."
        if cantidad <= 0:
            return False, "La cantidad debe ser mayor a cero."
        if precio_unitario <= 0:
            return False, "El precio unitario debe ser mayor a cero."
        total_linea = (Decimal(cantidad) * precio_unitario).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        lineas.append({
            'id_item': id_item,
            'cantidad': cantidad,
            'precio_unitario': precio_unitario,
            'total_linea': total_linea,
        })

    incluye = bool(incluye_igv)
    subtotal, igv, total = _calcular_totales_compra(lineas, incluye)

    if isinstance(fecha_compra, str):
        fecha_compra = fecha_compra.strip()[:10]
    try:
        partes = fecha_compra.split('-')
        fecha_dt = datetime(int(partes[0]), int(partes[1]), int(partes[2]))
    except (ValueError, IndexError, AttributeError):
        return False, "Fecha de compra no válida."

    nro_ref = (nro_comprobante_ref or '').strip() or None

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 FROM dbo.Inventario_Empresas WHERE IdEmpresa = ? AND EsProveedor = 1",
            (id_proveedor,),
        )
        if not cursor.fetchone():
            cursor.close()
            return False, "El proveedor seleccionado no existe o no es proveedor."

        cursor.execute(
            """
            INSERT INTO dbo.Inventario_ComprasCab (
                IdProveedor, FechaCompra, TipoComprobante, NroComprobanteRef,
                IncluyeIGV, SubTotal, IGV, Total,
                EstadoCompra, EstadoPago
            )
            OUTPUT INSERTED.IdCompra
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVA', ?)
            """,
            (
                id_proveedor,
                fecha_dt,
                tipo,
                nro_ref,
                1 if incluye else 0,
                float(subtotal),
                float(igv),
                float(total),
                estado_pago_val,
            ),
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False, "No se pudo registrar la compra."
        id_compra = int(row[0])

        for ln in lineas:
            cursor.execute(
                "SELECT 1 FROM dbo.Inventario_Items WHERE IdItem = ?",
                (ln['id_item'],),
            )
            if not cursor.fetchone():
                conn.rollback()
                return False, f"Artículo IdItem {ln['id_item']} no encontrado."

            cursor.execute(
                """
                INSERT INTO dbo.Inventario_ComprasDet (
                    IdCompra, IdItem, Cantidad, PrecioUnitario, TotalLinea
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    id_compra,
                    ln['id_item'],
                    ln['cantidad'],
                    float(ln['precio_unitario']),
                    float(ln['total_linea']),
                ),
            )
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual + ?
                WHERE IdItem = ?
                """,
                (ln['cantidad'], ln['id_item']),
            )

        conn.commit()
        cursor.close()
        return True, f"Compra registrada correctamente (N° {id_compra}). Stock actualizado."
    except Exception as e:
        _logger_db.exception("insertar_compra: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        err = str(e).lower()
        if 'invalid object name' in err and 'compras' in err:
            return False, (
                "Las tablas de compras no existen en la base de datos. "
                "Ejecute el script sql/Inventario_Compras.sql."
            )
        return False, "Error al registrar la compra. Verifique los datos e intente nuevamente."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def actualizar_compra(
    id_compra,
    id_proveedor,
    fecha_compra,
    tipo_comprobante,
    nro_comprobante_ref,
    incluye_igv,
    estado_pago,
    detalles,
):
    """Actualiza compra activa, recalcula detalle y ajusta stock."""
    from decimal import Decimal, ROUND_HALF_UP

    if not detalles:
        return False, "Debe agregar al menos un artículo al detalle."
    try:
        id_compra = int(id_compra)
        id_proveedor = int(id_proveedor)
    except (TypeError, ValueError):
        return False, "Compra o proveedor no válido."

    tipos_ok = ('FACTURA', 'BOLETA', 'NOTA_VENTA', 'NINGUNO')
    tipo = (tipo_comprobante or '').strip().upper()
    if tipo not in tipos_ok:
        return False, "Tipo de comprobante no válido."
    estado_pago_val = (estado_pago or 'PENDIENTE').strip().upper()
    if estado_pago_val not in ('PENDIENTE', 'CANCELADO'):
        return False, "Estado de pago no válido."

    lineas = []
    for d in detalles:
        try:
            id_item = int(d.get('id_item') or d.get('idItem'))
            cantidad = int(d.get('cantidad'))
            precio_unitario = Decimal(str(d.get('precio_unitario') or d.get('precioUnitario')))
        except (TypeError, ValueError, ArithmeticError):
            return False, "Línea de detalle con datos inválidos."
        if cantidad <= 0 or precio_unitario <= 0:
            return False, "Cantidad y precio unitario deben ser mayores a cero."
        total_linea = (Decimal(cantidad) * precio_unitario).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        lineas.append({
            'id_item': id_item,
            'cantidad': cantidad,
            'precio_unitario': precio_unitario,
            'total_linea': total_linea,
        })

    incluye = bool(incluye_igv)
    subtotal, igv, total = _calcular_totales_compra(lineas, incluye)

    if isinstance(fecha_compra, str):
        fecha_compra = fecha_compra.strip()[:10]
    try:
        partes = fecha_compra.split('-')
        fecha_dt = datetime(int(partes[0]), int(partes[1]), int(partes[2]))
    except (ValueError, IndexError, AttributeError):
        return False, "Fecha de compra no válida."

    nro_ref = (nro_comprobante_ref or '').strip() or None
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT EstadoCompra
            FROM dbo.Inventario_ComprasCab
            WHERE IdCompra = ?
            """,
            (id_compra,),
        )
        row_compra = cursor.fetchone()
        if not row_compra:
            cursor.close()
            return False, "La compra no existe."
        if str(row_compra[0] or '').strip().upper() == 'ANULADA':
            cursor.close()
            return False, "No se puede editar una compra anulada."

        cursor.execute(
            "SELECT 1 FROM dbo.Inventario_Empresas WHERE IdEmpresa = ? AND EsProveedor = 1",
            (id_proveedor,),
        )
        if not cursor.fetchone():
            cursor.close()
            return False, "El proveedor seleccionado no existe o no es proveedor."

        cursor.execute(
            "SELECT IdItem, Cantidad FROM dbo.Inventario_ComprasDet WHERE IdCompra = ?",
            (id_compra,),
        )
        old_det = cursor.fetchall()
        for od in old_det:
            od_item = int(od[0])
            od_qty = int(od[1] or 0)
            if od_qty <= 0:
                continue
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual - ?
                WHERE IdItem = ? AND StockActual >= ?
                """,
                (od_qty, od_item, od_qty),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                cursor.close()
                return False, "No hay stock suficiente para recalcular la edición de compra."

        cursor.execute("DELETE FROM dbo.Inventario_ComprasDet WHERE IdCompra = ?", (id_compra,))

        cursor.execute(
            """
            UPDATE dbo.Inventario_ComprasCab
            SET IdProveedor = ?, FechaCompra = ?, TipoComprobante = ?, NroComprobanteRef = ?,
                IncluyeIGV = ?, SubTotal = ?, IGV = ?, Total = ?, EstadoPago = ?
            WHERE IdCompra = ?
            """,
            (
                id_proveedor,
                fecha_dt,
                tipo,
                nro_ref,
                1 if incluye else 0,
                float(subtotal),
                float(igv),
                float(total),
                estado_pago_val,
                id_compra,
            ),
        )

        for ln in lineas:
            cursor.execute("SELECT 1 FROM dbo.Inventario_Items WHERE IdItem = ?", (ln['id_item'],))
            if not cursor.fetchone():
                conn.rollback()
                cursor.close()
                return False, f"Artículo IdItem {ln['id_item']} no encontrado."
            cursor.execute(
                """
                INSERT INTO dbo.Inventario_ComprasDet (
                    IdCompra, IdItem, Cantidad, PrecioUnitario, TotalLinea
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    id_compra,
                    ln['id_item'],
                    ln['cantidad'],
                    float(ln['precio_unitario']),
                    float(ln['total_linea']),
                ),
            )
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual + ?
                WHERE IdItem = ?
                """,
                (ln['cantidad'], ln['id_item']),
            )

        conn.commit()
        cursor.close()
        return True, f"Compra actualizada correctamente (N° {id_compra})."
    except Exception as e:
        _logger_db.exception("actualizar_compra: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al actualizar la compra."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def anular_compra(id_compra):
    """Anula una compra activa y revierte stock del detalle."""
    conn = None
    try:
        id_compra = int(id_compra)
    except (TypeError, ValueError):
        return False, "Compra no válida."
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT EstadoCompra FROM dbo.Inventario_ComprasCab WHERE IdCompra = ?",
            (id_compra,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return False, "La compra no existe."
        estado = str(row[0] or '').strip().upper()
        if estado == 'ANULADA':
            cursor.close()
            return False, "La compra ya está anulada."

        cursor.execute(
            "SELECT IdItem, Cantidad FROM dbo.Inventario_ComprasDet WHERE IdCompra = ?",
            (id_compra,),
        )
        detalles = cursor.fetchall()
        for det in detalles:
            id_item = int(det[0])
            cantidad = int(det[1] or 0)
            if cantidad <= 0:
                continue
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual - ?
                WHERE IdItem = ? AND StockActual >= ?
                """,
                (cantidad, id_item, cantidad),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                cursor.close()
                return False, "No hay stock suficiente para revertir la compra."

        cursor.execute(
            "UPDATE dbo.Inventario_ComprasCab SET EstadoCompra = 'ANULADA' WHERE IdCompra = ?",
            (id_compra,),
        )
        conn.commit()
        cursor.close()
        return True, f"Compra N° {id_compra} anulada correctamente."
    except Exception as e:
        _logger_db.exception("anular_compra: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al anular la compra."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_clientes_activos():
    """Clientes activos para selector de ventas."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdEmpresa, RUC, RazonSocial
            FROM dbo.Inventario_Empresas
            WHERE EsCliente = 1 AND Estado = 1
            ORDER BY RazonSocial
            """
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_clientes_activos: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_articulos_para_venta():
    """Artículos con stock para líneas de detalle en ventas."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT IdItem, Codigo, Descripcion, StockActual
            FROM dbo.Inventario_Items
            ORDER BY Descripcion, Codigo
            """
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_articulos_para_venta: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _calcular_totales_venta(detalles, incluye_igv):
    """Calcula subtotal, IGV y total para ventas (misma lógica que compras)."""
    return _calcular_totales_compra(detalles, incluye_igv)


def insertar_venta(
    id_cliente,
    fecha_venta,
    tipo_comprobante,
    nro_comprobante_ref,
    incluye_igv,
    estado_pago,
    detalles,
):
    """
    Registra cabecera y detalle de venta; descuenta stock por cada línea.
    detalles: lista de dicts con id_item, cantidad, precio_unitario.
    """
    from decimal import Decimal, ROUND_HALF_UP

    if not detalles:
        return False, "Debe agregar al menos un artículo al detalle."

    try:
        id_cliente = int(id_cliente)
    except (TypeError, ValueError):
        return False, "Cliente no válido."

    tipos_ok = ('FACTURA', 'BOLETA', 'NOTA_VENTA')
    tipo = (tipo_comprobante or '').strip().upper()
    if tipo not in tipos_ok:
        return False, "Tipo de comprobante no válido."

    estado_pago_val = (estado_pago or 'PENDIENTE').strip().upper()
    if estado_pago_val not in ('PENDIENTE', 'CANCELADO'):
        return False, "Estado de pago no válido."

    lineas = []
    for d in detalles:
        try:
            id_item = int(d.get('id_item') or d.get('idItem'))
            cantidad = int(d.get('cantidad'))
            precio_unitario = Decimal(str(d.get('precio_unitario') or d.get('precioUnitario')))
        except (TypeError, ValueError, ArithmeticError):
            return False, "Línea de detalle con datos inválidos."
        if cantidad <= 0:
            return False, "La cantidad debe ser mayor a cero."
        if precio_unitario <= 0:
            return False, "El precio unitario debe ser mayor a cero."
        total_linea = (Decimal(cantidad) * precio_unitario).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        lineas.append({
            'id_item': id_item,
            'cantidad': cantidad,
            'precio_unitario': precio_unitario,
            'total_linea': total_linea,
        })

    incluye = bool(incluye_igv)
    subtotal, igv, total = _calcular_totales_venta(lineas, incluye)

    if isinstance(fecha_venta, str):
        fecha_venta = fecha_venta.strip()[:10]
    try:
        partes = fecha_venta.split('-')
        fecha_dt = datetime(int(partes[0]), int(partes[1]), int(partes[2]))
    except (ValueError, IndexError, AttributeError):
        return False, "Fecha de venta no válida."

    nro_ref = (nro_comprobante_ref or '').strip() or None

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 FROM dbo.Inventario_Empresas WHERE IdEmpresa = ? AND EsCliente = 1",
            (id_cliente,),
        )
        if not cursor.fetchone():
            cursor.close()
            return False, "El cliente seleccionado no existe o no es cliente."

        for ln in lineas:
            cursor.execute(
                "SELECT StockActual FROM dbo.Inventario_Items WHERE IdItem = ?",
                (ln['id_item'],),
            )
            row_stock = cursor.fetchone()
            if not row_stock:
                conn.rollback()
                cursor.close()
                return False, f"Artículo IdItem {ln['id_item']} no encontrado."
            stock_disp = int(row_stock[0] or 0)
            if stock_disp < ln['cantidad']:
                conn.rollback()
                cursor.close()
                return False, (
                    f"Stock insuficiente para el artículo IdItem {ln['id_item']}. "
                    f"Disponible: {stock_disp}, solicitado: {ln['cantidad']}."
                )

        cursor.execute(
            """
            INSERT INTO dbo.Inventario_VentasCab (
                IdCliente, FechaVenta, TipoComprobante, NroComprobanteRef,
                IncluyeIGV, SubTotal, IGV, Total,
                EstadoVenta, EstadoPago
            )
            OUTPUT INSERTED.IdVenta
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVA', ?)
            """,
            (
                id_cliente,
                fecha_dt,
                tipo,
                nro_ref,
                1 if incluye else 0,
                float(subtotal),
                float(igv),
                float(total),
                estado_pago_val,
            ),
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False, "No se pudo registrar la venta."
        id_venta = int(row[0])

        for ln in lineas:
            cursor.execute(
                """
                INSERT INTO dbo.Inventario_VentasDet (
                    IdVenta, IdItem, Cantidad, PrecioUnitario, TotalLinea
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    id_venta,
                    ln['id_item'],
                    ln['cantidad'],
                    float(ln['precio_unitario']),
                    float(ln['total_linea']),
                ),
            )
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual - ?
                WHERE IdItem = ? AND StockActual >= ?
                """,
                (ln['cantidad'], ln['id_item'], ln['cantidad']),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                cursor.close()
                return False, f"Stock insuficiente al despachar el artículo IdItem {ln['id_item']}."

        conn.commit()
        cursor.close()
        return True, f"Venta registrada correctamente (N° {id_venta}). Stock actualizado."
    except Exception as e:
        _logger_db.exception("insertar_venta: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        err = str(e).lower()
        if 'invalid object name' in err and 'ventas' in err:
            return False, (
                "Las tablas de ventas no existen en la base de datos. "
                "Ejecute el script sql/Inventario_Ventas.sql."
            )
        return False, "Error al registrar la venta. Verifique los datos e intente nuevamente."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_venta_por_id(id_venta):
    """Obtiene cabecera y detalle de una venta para edición."""
    conn = None
    try:
        id_venta_i = int(id_venta)
    except (TypeError, ValueError):
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                v.IdVenta, v.IdCliente, v.FechaVenta, v.TipoComprobante, v.NroComprobanteRef,
                v.IncluyeIGV, v.EstadoPago, v.EstadoVenta
            FROM dbo.Inventario_VentasCab v
            WHERE v.IdVenta = ?
            """,
            (id_venta_i,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return None
        columns = [col[0] for col in cursor.description]
        cab = {k.lower(): v for k, v in dict(zip(columns, row)).items()}

        cursor.execute(
            """
            SELECT
                d.IdItem, i.Codigo, i.Descripcion, d.Cantidad, d.PrecioUnitario, d.TotalLinea
            FROM dbo.Inventario_VentasDet d
            INNER JOIN dbo.Inventario_Items i ON d.IdItem = i.IdItem
            WHERE d.IdVenta = ?
            ORDER BY d.IdVentaDet
            """,
            (id_venta_i,),
        )
        det_cols = [col[0] for col in cursor.description]
        detalles = []
        for det in cursor.fetchall():
            item = {k.lower(): v for k, v in dict(zip(det_cols, det)).items()}
            detalles.append(item)
        cursor.close()
        cab['detalles'] = detalles
        return cab
    except Exception as e:
        _logger_db.exception("get_venta_por_id: %s", e)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def actualizar_venta(
    id_venta,
    id_cliente,
    fecha_venta,
    tipo_comprobante,
    nro_comprobante_ref,
    incluye_igv,
    estado_pago,
    detalles,
):
    """Actualiza venta activa, recalcula detalle y ajusta stock."""
    from decimal import Decimal, ROUND_HALF_UP

    if not detalles:
        return False, "Debe agregar al menos un artículo al detalle."
    try:
        id_venta = int(id_venta)
        id_cliente = int(id_cliente)
    except (TypeError, ValueError):
        return False, "Venta o cliente no válido."

    tipos_ok = ('FACTURA', 'BOLETA', 'NOTA_VENTA')
    tipo = (tipo_comprobante or '').strip().upper()
    if tipo not in tipos_ok:
        return False, "Tipo de comprobante no válido."
    estado_pago_val = (estado_pago or 'PENDIENTE').strip().upper()
    if estado_pago_val not in ('PENDIENTE', 'CANCELADO'):
        return False, "Estado de pago no válido."

    lineas = []
    for d in detalles:
        try:
            id_item = int(d.get('id_item') or d.get('idItem'))
            cantidad = int(d.get('cantidad'))
            precio_unitario = Decimal(str(d.get('precio_unitario') or d.get('precioUnitario')))
        except (TypeError, ValueError, ArithmeticError):
            return False, "Línea de detalle con datos inválidos."
        if cantidad <= 0 or precio_unitario <= 0:
            return False, "Cantidad y precio unitario deben ser mayores a cero."
        total_linea = (Decimal(cantidad) * precio_unitario).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        lineas.append({
            'id_item': id_item,
            'cantidad': cantidad,
            'precio_unitario': precio_unitario,
            'total_linea': total_linea,
        })

    incluye = bool(incluye_igv)
    subtotal, igv, total = _calcular_totales_venta(lineas, incluye)

    if isinstance(fecha_venta, str):
        fecha_venta = fecha_venta.strip()[:10]
    try:
        partes = fecha_venta.split('-')
        fecha_dt = datetime(int(partes[0]), int(partes[1]), int(partes[2]))
    except (ValueError, IndexError, AttributeError):
        return False, "Fecha de venta no válida."

    nro_ref = (nro_comprobante_ref or '').strip() or None
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT EstadoVenta
            FROM dbo.Inventario_VentasCab
            WHERE IdVenta = ?
            """,
            (id_venta,),
        )
        row_venta = cursor.fetchone()
        if not row_venta:
            cursor.close()
            return False, "La venta no existe."
        if str(row_venta[0] or '').strip().upper() == 'ANULADA':
            cursor.close()
            return False, "No se puede editar una venta anulada."

        cursor.execute(
            "SELECT 1 FROM dbo.Inventario_Empresas WHERE IdEmpresa = ? AND EsCliente = 1",
            (id_cliente,),
        )
        if not cursor.fetchone():
            cursor.close()
            return False, "El cliente seleccionado no existe o no es cliente."

        cursor.execute(
            "SELECT IdItem, Cantidad FROM dbo.Inventario_VentasDet WHERE IdVenta = ?",
            (id_venta,),
        )
        old_det = cursor.fetchall()
        for od in old_det:
            od_item = int(od[0])
            od_qty = int(od[1] or 0)
            if od_qty <= 0:
                continue
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual + ?
                WHERE IdItem = ?
                """,
                (od_qty, od_item),
            )

        cursor.execute("DELETE FROM dbo.Inventario_VentasDet WHERE IdVenta = ?", (id_venta,))

        cursor.execute(
            """
            UPDATE dbo.Inventario_VentasCab
            SET IdCliente = ?, FechaVenta = ?, TipoComprobante = ?, NroComprobanteRef = ?,
                IncluyeIGV = ?, SubTotal = ?, IGV = ?, Total = ?, EstadoPago = ?
            WHERE IdVenta = ?
            """,
            (
                id_cliente,
                fecha_dt,
                tipo,
                nro_ref,
                1 if incluye else 0,
                float(subtotal),
                float(igv),
                float(total),
                estado_pago_val,
                id_venta,
            ),
        )

        for ln in lineas:
            cursor.execute("SELECT 1 FROM dbo.Inventario_Items WHERE IdItem = ?", (ln['id_item'],))
            if not cursor.fetchone():
                conn.rollback()
                cursor.close()
                return False, f"Artículo IdItem {ln['id_item']} no encontrado."

            cursor.execute(
                "SELECT StockActual FROM dbo.Inventario_Items WHERE IdItem = ?",
                (ln['id_item'],),
            )
            row_stock = cursor.fetchone()
            stock_disp = int(row_stock[0] or 0) if row_stock else 0
            if stock_disp < ln['cantidad']:
                conn.rollback()
                cursor.close()
                return False, (
                    f"Stock insuficiente para el artículo IdItem {ln['id_item']}. "
                    f"Disponible: {stock_disp}, solicitado: {ln['cantidad']}."
                )

            cursor.execute(
                """
                INSERT INTO dbo.Inventario_VentasDet (
                    IdVenta, IdItem, Cantidad, PrecioUnitario, TotalLinea
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    id_venta,
                    ln['id_item'],
                    ln['cantidad'],
                    float(ln['precio_unitario']),
                    float(ln['total_linea']),
                ),
            )
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual - ?
                WHERE IdItem = ? AND StockActual >= ?
                """,
                (ln['cantidad'], ln['id_item'], ln['cantidad']),
            )
            if cursor.rowcount == 0:
                conn.rollback()
                cursor.close()
                return False, f"Stock insuficiente al despachar el artículo IdItem {ln['id_item']}."

        conn.commit()
        cursor.close()
        return True, f"Venta actualizada correctamente (N° {id_venta})."
    except Exception as e:
        _logger_db.exception("actualizar_venta: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al actualizar la venta."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_lista_ventas_inventario(codigo='', articulo='', cliente=0):
    """Ejecuta sp_inv_lista_ventas con filtros."""
    conn = None
    codigo_s = (codigo or '').strip()
    articulo_s = (articulo or '').strip()
    try:
        cliente_i = int(cliente or 0)
    except (TypeError, ValueError):
        cliente_i = 0
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_inv_lista_ventas @codigo=?, @articulo=?, @cliente=?",
            (codigo_s, articulo_s, cliente_i),
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_lista_ventas_inventario: %s", e)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def anular_venta(id_venta):
    """Anula una venta activa y devuelve el stock del detalle al almacén."""
    conn = None
    try:
        id_venta = int(id_venta)
    except (TypeError, ValueError):
        return False, "Venta no válida."
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT EstadoVenta FROM dbo.Inventario_VentasCab WHERE IdVenta = ?",
            (id_venta,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return False, "La venta no existe."
        estado = str(row[0] or '').strip().upper()
        if estado == 'ANULADA':
            cursor.close()
            return False, "La venta ya está anulada."

        cursor.execute(
            "SELECT IdItem, Cantidad FROM dbo.Inventario_VentasDet WHERE IdVenta = ?",
            (id_venta,),
        )
        detalles = cursor.fetchall()
        for det in detalles:
            id_item = int(det[0])
            cantidad = int(det[1] or 0)
            if cantidad <= 0:
                continue
            cursor.execute(
                """
                UPDATE dbo.Inventario_Items
                SET StockActual = StockActual + ?
                WHERE IdItem = ?
                """,
                (cantidad, id_item),
            )

        cursor.execute(
            "UPDATE dbo.Inventario_VentasCab SET EstadoVenta = 'ANULADA' WHERE IdVenta = ?",
            (id_venta,),
        )
        conn.commit()
        cursor.close()
        return True, f"Venta N° {id_venta} anulada correctamente."
    except Exception as e:
        _logger_db.exception("anular_venta: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al anular la venta."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


PROFORMA_COMPANY_CORRELATIVO = 'BGT'


def _formatear_nro_proforma(correlativo):
    """Formatea correlativo numérico a 6 dígitos (ej. 96 -> '000096')."""
    try:
        n = int(correlativo)
    except (TypeError, ValueError):
        n = 0
    return str(n).zfill(6)


def _lineas_proforma_desde_detalles(detalles):
    """Valida detalle y devuelve (lineas, total) o (None, None, mensaje_error)."""
    from decimal import Decimal, ROUND_HALF_UP

    if not detalles:
        return None, None, "Debe agregar al menos un artículo al detalle."

    lineas = []
    for d in detalles:
        try:
            id_item = int(d.get('id_item') or d.get('idItem'))
            cantidad = int(d.get('cantidad'))
            precio_unitario = Decimal(str(d.get('precio_unitario') or d.get('precioUnitario')))
        except (TypeError, ValueError, ArithmeticError):
            return None, None, "Línea de detalle con datos inválidos."
        if cantidad <= 0:
            return None, None, "La cantidad debe ser mayor a cero."
        if precio_unitario <= 0:
            return None, None, "El precio unitario debe ser mayor a cero."
        total_linea = (Decimal(cantidad) * precio_unitario).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        lineas.append({
            'id_item': id_item,
            'cantidad': cantidad,
            'precio_unitario': precio_unitario,
            'total_linea': total_linea,
        })

    total = sum((ln['total_linea'] for ln in lineas), Decimal('0.00'))
    return lineas, total, None


def _fecha_proforma_a_datetime(fecha_proforma):
    if isinstance(fecha_proforma, str):
        fecha_proforma = fecha_proforma.strip()[:10]
    try:
        partes = fecha_proforma.split('-')
        return datetime(int(partes[0]), int(partes[1]), int(partes[2]))
    except (ValueError, IndexError, AttributeError):
        return None


def insertar_proforma(id_cliente, fecha_proforma, detalles, company=PROFORMA_COMPANY_CORRELATIVO):
    """
    Registra cabecera y detalle de proforma; incrementa CorrelativoProforma en PR_mapping2.
    No descuenta stock. detalles: lista de dicts con id_item, cantidad, precio_unitario.
    """
    try:
        id_cliente = int(id_cliente)
    except (TypeError, ValueError):
        return False, "Cliente no válido."

    lineas, total, err = _lineas_proforma_desde_detalles(detalles)
    if err:
        return False, err

    fecha_dt = _fecha_proforma_a_datetime(fecha_proforma)
    if not fecha_dt:
        return False, "Fecha de proforma no válida."

    company = (company or PROFORMA_COMPANY_CORRELATIVO).strip() or PROFORMA_COMPANY_CORRELATIVO

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 FROM dbo.Inventario_Empresas WHERE IdEmpresa = ? AND EsCliente = 1",
            (id_cliente,),
        )
        if not cursor.fetchone():
            cursor.close()
            return False, "El cliente seleccionado no existe o no es cliente."

        for ln in lineas:
            cursor.execute(
                "SELECT 1 FROM dbo.Inventario_Items WHERE IdItem = ?",
                (ln['id_item'],),
            )
            if not cursor.fetchone():
                cursor.close()
                return False, f"Artículo IdItem {ln['id_item']} no encontrado."

        cursor.execute(
            """
            UPDATE dbo.PR_mapping2
            SET CorrelativoProforma = CorrelativoProforma + 1
            OUTPUT INSERTED.CorrelativoProforma
            WHERE company = ?
            """,
            (company,),
        )
        row_corr = cursor.fetchone()
        if not row_corr or row_corr[0] is None:
            conn.rollback()
            cursor.close()
            return False, f"No se encontró correlativo de proforma para la compañía {company}."
        nro_proforma = _formatear_nro_proforma(row_corr[0])

        cursor.execute(
            """
            INSERT INTO dbo.Inventario_ProformasCab (
                NroProforma, IdCliente, FechaProforma, Total
            )
            OUTPUT INSERTED.IdProforma
            VALUES (?, ?, ?, ?)
            """,
            (
                nro_proforma,
                id_cliente,
                fecha_dt,
                float(total),
            ),
        )
        row_cab = cursor.fetchone()
        if not row_cab:
            conn.rollback()
            cursor.close()
            return False, "No se pudo registrar la proforma."
        id_proforma = int(row_cab[0])

        for ln in lineas:
            cursor.execute(
                """
                INSERT INTO dbo.Inventario_ProformasDet (
                    IdProforma, IdItem, Cantidad, PrecioUnitario, TotalLinea
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    id_proforma,
                    ln['id_item'],
                    ln['cantidad'],
                    float(ln['precio_unitario']),
                    float(ln['total_linea']),
                ),
            )

        conn.commit()
        cursor.close()
        return True, f"Proforma N° {nro_proforma} registrada correctamente."
    except pyodbc.IntegrityError as e:
        err = str(e).lower()
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        if "uq_proformas_nro" in err or "unique" in err:
            return False, "El número de proforma ya existe. Intente guardar nuevamente."
        if "fk_proformas" in err:
            return False, "Datos de referencia inválidos (cliente o artículo)."
        return False, "No se pudo guardar la proforma: datos duplicados o referencia inválida."
    except Exception as e:
        _logger_db.exception("insertar_proforma: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        msg = str(e)
        if 'correlativo de proforma' in msg.lower():
            return False, f"No se encontró correlativo de proforma para la compañía {company}."
        return False, "Error al guardar la proforma. Verifique la conexión a la base de datos."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_proforma_por_id(id_proforma):
    """Obtiene cabecera y detalle de una proforma para edición."""
    conn = None
    try:
        id_proforma_i = int(id_proforma)
    except (TypeError, ValueError):
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                p.IdProforma, p.NroProforma, p.IdCliente, p.FechaProforma, p.Total,
                e.RazonSocial, e.RUC, e.Direccion
            FROM dbo.Inventario_ProformasCab p
            INNER JOIN dbo.Inventario_Empresas e ON p.IdCliente = e.IdEmpresa
            WHERE p.IdProforma = ?
            """,
            (id_proforma_i,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return None
        columns = [col[0] for col in cursor.description]
        cab = {k.lower(): v for k, v in dict(zip(columns, row)).items()}

        cursor.execute(
            """
            SELECT d.IdItem, i.Codigo, i.Descripcion, d.Cantidad, d.PrecioUnitario, d.TotalLinea
            FROM dbo.Inventario_ProformasDet d
            INNER JOIN dbo.Inventario_Items i ON d.IdItem = i.IdItem
            WHERE d.IdProforma = ?
            ORDER BY d.IdProformaDet
            """,
            (id_proforma_i,),
        )
        det_cols = [col[0] for col in cursor.description]
        detalles = []
        for det in cursor.fetchall():
            item = {k.lower(): v for k, v in dict(zip(det_cols, det)).items()}
            detalles.append(item)
        cursor.close()
        cab['detalles'] = detalles
        return cab
    except Exception as e:
        _logger_db.exception("get_proforma_por_id: %s", e)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def actualizar_proforma(id_proforma, id_cliente, fecha_proforma, detalles):
    """Actualiza cabecera y detalle de proforma (sin cambiar NroProforma ni correlativo)."""
    try:
        id_proforma = int(id_proforma)
        id_cliente = int(id_cliente)
    except (TypeError, ValueError):
        return False, "Proforma o cliente no válido."

    lineas, total, err = _lineas_proforma_desde_detalles(detalles)
    if err:
        return False, err

    fecha_dt = _fecha_proforma_a_datetime(fecha_proforma)
    if not fecha_dt:
        return False, "Fecha de proforma no válida."

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT NroProforma FROM dbo.Inventario_ProformasCab WHERE IdProforma = ?",
            (id_proforma,),
        )
        row_cab = cursor.fetchone()
        if not row_cab:
            cursor.close()
            return False, "Proforma no encontrada."
        nro_proforma = row_cab[0]

        cursor.execute(
            "SELECT 1 FROM dbo.Inventario_Empresas WHERE IdEmpresa = ? AND EsCliente = 1",
            (id_cliente,),
        )
        if not cursor.fetchone():
            cursor.close()
            return False, "El cliente seleccionado no existe o no es cliente."

        for ln in lineas:
            cursor.execute(
                "SELECT 1 FROM dbo.Inventario_Items WHERE IdItem = ?",
                (ln['id_item'],),
            )
            if not cursor.fetchone():
                cursor.close()
                return False, f"Artículo IdItem {ln['id_item']} no encontrado."

        cursor.execute(
            """
            UPDATE dbo.Inventario_ProformasCab
            SET IdCliente = ?, FechaProforma = ?, Total = ?
            WHERE IdProforma = ?
            """,
            (id_cliente, fecha_dt, float(total), id_proforma),
        )

        cursor.execute(
            "DELETE FROM dbo.Inventario_ProformasDet WHERE IdProforma = ?",
            (id_proforma,),
        )

        for ln in lineas:
            cursor.execute(
                """
                INSERT INTO dbo.Inventario_ProformasDet (
                    IdProforma, IdItem, Cantidad, PrecioUnitario, TotalLinea
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    id_proforma,
                    ln['id_item'],
                    ln['cantidad'],
                    float(ln['precio_unitario']),
                    float(ln['total_linea']),
                ),
            )

        conn.commit()
        cursor.close()
        return True, f"Proforma N° {nro_proforma} actualizada correctamente."
    except pyodbc.IntegrityError as e:
        err = str(e).lower()
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        if "fk_proformas" in err:
            return False, "Datos de referencia inválidos (cliente o artículo)."
        return False, "No se pudo actualizar la proforma: referencia inválida."
    except Exception as e:
        _logger_db.exception("actualizar_proforma: %s", e)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False, "Error al actualizar la proforma. Verifique la conexión a la base de datos."
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_lista_proformas_inventario(codigo='', articulo='', cliente=0):
    """Ejecuta sp_inv_lista_proformas con filtros."""
    conn = None
    codigo_s = (codigo or '').strip()
    articulo_s = (articulo or '').strip()
    try:
        cliente_i = int(cliente or 0)
    except (TypeError, ValueError):
        cliente_i = 0
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "EXEC sp_inv_lista_proformas @codigo=?, @articulo=?, @cliente=?",
            (codigo_s, articulo_s, cliente_i),
        )
        columns = [col[0] for col in cursor.description]
        rows = []
        for row in cursor.fetchall():
            item = {col: val for col, val in zip(columns, row)}
            rows.append({k.lower(): v for k, v in item.items()})
        cursor.close()
        return rows
    except Exception as e:
        _logger_db.exception("get_lista_proformas_inventario: %s", e)
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

