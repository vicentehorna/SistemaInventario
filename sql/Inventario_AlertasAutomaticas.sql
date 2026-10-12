-- Alerta automática diaria de cobranza por WhatsApp.
-- Inventario_AlertasConfig: configuración por empresa (activo, hora, días). La edita la app (Render).
-- Inventario_AlertasEnvio: clientes avisados por día; evita enviar dos alertas el mismo día al mismo cliente.
-- El script alertas_vencimiento.py --atender-solicitudes (tarea programada cada minuto en el servidor)
-- lee la configuración, registra la solicitud AUTOMATICA y la atiende.

IF OBJECT_ID('dbo.Inventario_AlertasConfig', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Inventario_AlertasConfig (
        Company VARCHAR(4) NOT NULL,
        Activo BIT NOT NULL CONSTRAINT DF_AlertasCfg_Activo DEFAULT 0,
        HoraEnvio CHAR(5) NOT NULL CONSTRAINT DF_AlertasCfg_Hora DEFAULT '09:00',
        -- Un carácter por día, de lunes a domingo: '1' = enviar, '0' = no enviar.
        DiasSemana CHAR(7) NOT NULL CONSTRAINT DF_AlertasCfg_Dias DEFAULT '1111110',
        -- Primera activación: solo se avisan ventas que vencen desde esta fecha.
        FechaActivacion DATETIME NULL,
        -- Día del último envío automático programado (evita repetirlo el mismo día).
        UltimoEnvio DATETIME NULL,
        XLastUser VARCHAR(50) NULL,
        XLastDate DATETIME NULL,
        CONSTRAINT PK_Inventario_AlertasConfig PRIMARY KEY CLUSTERED (Company)
    );
END
GO

IF OBJECT_ID('dbo.Inventario_AlertasEnvio', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Inventario_AlertasEnvio (
        IdEnvio INT IDENTITY(1,1) NOT NULL,
        Company VARCHAR(4) NOT NULL,
        Fecha DATETIME NOT NULL,
        IdCliente INT NOT NULL,
        Telefono VARCHAR(15) NOT NULL,
        Comprobantes INT NOT NULL,
        IdSolicitud INT NULL,
        FechaEnvio DATETIME NOT NULL,
        CONSTRAINT PK_Inventario_AlertasEnvio PRIMARY KEY CLUSTERED (IdEnvio),
        CONSTRAINT UQ_AlertasEnvio_Dia UNIQUE (Company, Fecha, IdCliente)
    );
END
GO
