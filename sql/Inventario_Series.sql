-- Series y correlativos de documentos internos (una serie activa por tipo de documento)

IF OBJECT_ID('dbo.Inventario_Series', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Inventario_Series (
        IdSerie INT IDENTITY(1,1) NOT NULL,
        TipoDocumento VARCHAR(5) NOT NULL,
        Descripcion VARCHAR(50) NOT NULL,
        Serie VARCHAR(10) NOT NULL,
        UltimoNumero INT NOT NULL CONSTRAINT DF_Series_UltimoNumero DEFAULT 0,
        Longitud TINYINT NOT NULL CONSTRAINT DF_Series_Longitud DEFAULT 8,
        Activo BIT NOT NULL CONSTRAINT DF_Series_Activo DEFAULT 1,
        FechaRegistro DATETIME CONSTRAINT DF_Series_FechaReg DEFAULT GETDATE(),
        CONSTRAINT PK_Inventario_Series PRIMARY KEY CLUSTERED (IdSerie),
        CONSTRAINT UQ_Series_Tipo_Serie UNIQUE (TipoDocumento, Serie),
        CONSTRAINT CK_Series_Tipo CHECK (TipoDocumento IN ('NV', 'PF', 'RC', 'RP', 'NI')),
        CONSTRAINT CK_Series_UltimoNumero CHECK (UltimoNumero >= 0),
        CONSTRAINT CK_Series_Longitud CHECK (Longitud BETWEEN 4 AND 10)
    );
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'UX_Series_TipoActivo'
      AND object_id = OBJECT_ID('dbo.Inventario_Series')
)
    CREATE UNIQUE NONCLUSTERED INDEX UX_Series_TipoActivo
        ON dbo.Inventario_Series (TipoDocumento)
        WHERE Activo = 1;
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Inventario_Series WHERE TipoDocumento = 'NV')
    INSERT INTO dbo.Inventario_Series (TipoDocumento, Descripcion, Serie, Longitud)
    VALUES ('NV', 'Nota de Venta', 'NV01', 8);

IF NOT EXISTS (SELECT 1 FROM dbo.Inventario_Series WHERE TipoDocumento = 'RC')
    INSERT INTO dbo.Inventario_Series (TipoDocumento, Descripcion, Serie, Longitud)
    VALUES ('RC', 'Recibo de Cobro', 'RC01', 8);

IF NOT EXISTS (SELECT 1 FROM dbo.Inventario_Series WHERE TipoDocumento = 'RP')
    INSERT INTO dbo.Inventario_Series (TipoDocumento, Descripcion, Serie, Longitud)
    VALUES ('RP', 'Recibo de Pago', 'RP01', 8);
GO

-- Proforma: continúa la numeración existente (PR_mapping2 y proformas ya emitidas), formato de 6 dígitos
IF NOT EXISTS (SELECT 1 FROM dbo.Inventario_Series WHERE TipoDocumento = 'PF')
BEGIN
    DECLARE @UltimoMapping INT = 0;
    DECLARE @UltimoEmitido INT = 0;

    IF OBJECT_ID('dbo.PR_mapping2', 'U') IS NOT NULL
        EXEC sp_executesql
            N'SELECT @n = ISNULL(MAX(CorrelativoProforma), 0) FROM dbo.PR_mapping2 WHERE company = ''BGT''',
            N'@n INT OUTPUT',
            @n = @UltimoMapping OUTPUT;

    IF OBJECT_ID('dbo.Inventario_ProformasCab', 'U') IS NOT NULL
        SELECT @UltimoEmitido = ISNULL(MAX(TRY_CAST(NroProforma AS INT)), 0)
        FROM dbo.Inventario_ProformasCab;

    INSERT INTO dbo.Inventario_Series (TipoDocumento, Descripcion, Serie, UltimoNumero, Longitud)
    VALUES (
        'PF', 'Proforma', 'PF01',
        CASE WHEN @UltimoMapping > @UltimoEmitido THEN @UltimoMapping ELSE @UltimoEmitido END,
        6
    );
END
GO
