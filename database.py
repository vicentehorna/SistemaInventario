import logging
import os
import pyodbc
import platform
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
    def get_connection():
        """Crea y retorna una conexión a SQL Server"""
        if platform.system() == 'Windows':
            try:
                return pyodbc.connect(DatabaseConfig.get_connection_string())
            except Exception as e:
                print(f"Error al conectar con SQL Server: {e}")
                raise

        # Linux: probar 18 primero y luego 17 para mayor compatibilidad.
        candidate_drivers = [
            os.getenv('SQL_ODBC_DRIVER', '').strip(),
            'ODBC Driver 18 for SQL Server',
            'ODBC Driver 17 for SQL Server',
        ]
        seen = set()
        last_error = None
        for drv in candidate_drivers:
            if not drv or drv in seen:
                continue
            seen.add(drv)
            try:
                return pyodbc.connect(DatabaseConfig.get_connection_string(driver_override=drv))
            except Exception as e:
                last_error = e
                print(f"DEBUG: Falló conexión con driver '{drv}': {e}")

        print(f"Error al conectar con SQL Server: {last_error}")
        raise last_error


def get_db_connection():
    """Conexión pyodbc reutilizable (APIs, reportes)."""
    return DatabaseConfig.get_connection()


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
    def validate_user(username, password):
        """
        Valida las credenciales del usuario contra la base de datos.

        Si existe fila en SY_User + SY_UserProfile con Profile = 'GENERAL' para el UserID,
        se valida con la consulta sin PR_Employee; en caso contrario se usa la consulta
        original (empleado activo Status = 'N').
        """
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
                Aplicacion, CodigoBomba, StockActual
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                codigo,
                int(id_categoria),
                int(id_marca),
                descripcion,
                (aplicacion or "").strip() or None,
                (codigo_bomba or "").strip() or None,
                stock,
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
                Aplicacion = ?, CodigoBomba = ?, StockActual = ?
            WHERE IdItem = ?
            """,
            (
                int(id_categoria),
                int(id_marca),
                descripcion,
                (aplicacion or "").strip() or None,
                (codigo_bomba or "").strip() or None,
                stock,
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

