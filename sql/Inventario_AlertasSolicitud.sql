-- Solicitudes de envío de alertas por WhatsApp.
-- La app (Render) registra la solicitud; el script alertas_vencimiento.py --atender-solicitudes,
-- que corre en el servidor junto a Evolution API, la atiende y guarda el resultado.

IF OBJECT_ID('dbo.Inventario_AlertasSolicitud', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Inventario_AlertasSolicitud (
        IdSolicitud INT IDENTITY(1,1) NOT NULL,
        Company VARCHAR(4) NOT NULL,
        Tipo VARCHAR(20) NOT NULL CONSTRAINT DF_AlertasSol_Tipo DEFAULT 'CLIENTES_HOY',
        Estado VARCHAR(15) NOT NULL CONSTRAINT DF_AlertasSol_Estado DEFAULT 'PENDIENTE',
        Usuario VARCHAR(50) NULL,
        FechaSolicitud DATETIME NOT NULL,
        FechaInicio DATETIME NULL,
        FechaFin DATETIME NULL,
        Enviados INT NOT NULL CONSTRAINT DF_AlertasSol_Enviados DEFAULT 0,
        Fallidos INT NOT NULL CONSTRAINT DF_AlertasSol_Fallidos DEFAULT 0,
        SinCelular INT NOT NULL CONSTRAINT DF_AlertasSol_SinCelular DEFAULT 0,
        Resultado NVARCHAR(MAX) NULL,
        CONSTRAINT PK_Inventario_AlertasSolicitud PRIMARY KEY CLUSTERED (IdSolicitud)
    );
    CREATE INDEX IX_AlertasSol_Estado ON dbo.Inventario_AlertasSolicitud (Estado, IdSolicitud);
END
GO
