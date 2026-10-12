-- Nota de venta interna (serie y correlativo) y comprobante SUNAT opcional en ventas.
-- Requiere sql/Inventario_Series.sql (serie activa de tipo NV).
--   TipoComprobante: NINGUNO, BOLETA o FACTURA (comprobante SUNAT emitido fuera del sistema).
--   NroComprobanteRef: referencia libre (pedido del cliente, nota física anterior, etc.).

IF COL_LENGTH('dbo.Inventario_VentasCab', 'IdSerie') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab ADD IdSerie INT NULL;
IF COL_LENGTH('dbo.Inventario_VentasCab', 'Serie') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab ADD Serie VARCHAR(10) NULL;
IF COL_LENGTH('dbo.Inventario_VentasCab', 'Numero') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab ADD Numero INT NULL;
IF COL_LENGTH('dbo.Inventario_VentasCab', 'NroVenta') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab ADD NroVenta VARCHAR(25) NULL;
IF COL_LENGTH('dbo.Inventario_VentasCab', 'SerieComprobante') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab ADD SerieComprobante VARCHAR(4) NULL;
IF COL_LENGTH('dbo.Inventario_VentasCab', 'NumeroComprobante') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab ADD NumeroComprobante INT NULL;
GO

-- Boletas y facturas con referencia "SERIE-NUMERO" (ej. E001-702): se separa serie y número
UPDATE dbo.Inventario_VentasCab
SET SerieComprobante = UPPER(LEFT(LTRIM(RTRIM(NroComprobanteRef)), 4)),
    NumeroComprobante = TRY_CAST(SUBSTRING(LTRIM(RTRIM(NroComprobanteRef)), 6, 20) AS INT),
    NroComprobanteRef = NULL
WHERE TipoComprobante IN ('FACTURA', 'BOLETA')
  AND SerieComprobante IS NULL
  AND LTRIM(RTRIM(NroComprobanteRef)) LIKE '[A-Za-z0-9][A-Za-z0-9][A-Za-z0-9][A-Za-z0-9]-%'
  AND TRY_CAST(SUBSTRING(LTRIM(RTRIM(NroComprobanteRef)), 6, 20) AS INT) > 0;

-- El resto no tiene comprobante SUNAT; su referencia (si tenía) se conserva en NroComprobanteRef
UPDATE dbo.Inventario_VentasCab
SET TipoComprobante = 'NINGUNO',
    SerieComprobante = NULL,
    NumeroComprobante = NULL
WHERE TipoComprobante NOT IN ('FACTURA', 'BOLETA')
   OR SerieComprobante IS NULL
   OR NumeroComprobante IS NULL;
GO

-- Numeración de las ventas existentes en orden cronológico (fecha de venta, luego orden de registro)
SET XACT_ABORT ON;
BEGIN TRANSACTION;

DECLARE @IdSerie INT, @Serie VARCHAR(10), @Ultimo INT, @Longitud INT;

SELECT @IdSerie = IdSerie, @Serie = Serie, @Ultimo = UltimoNumero, @Longitud = Longitud
FROM dbo.Inventario_Series WITH (UPDLOCK, HOLDLOCK)
WHERE TipoDocumento = 'NV' AND Activo = 1;

IF @IdSerie IS NULL
BEGIN
    ROLLBACK TRANSACTION;
    RAISERROR('No hay una serie activa de Nota de Venta. Ejecute primero sql/Inventario_Series.sql.', 16, 1);
END
ELSE
BEGIN
    ;WITH pendientes AS (
        SELECT IdSerie, Serie, Numero, NroVenta,
               @Ultimo + ROW_NUMBER() OVER (ORDER BY FechaVenta, IdVenta) AS NuevoNumero
        FROM dbo.Inventario_VentasCab
        WHERE Numero IS NULL
    )
    UPDATE pendientes
    SET IdSerie = @IdSerie,
        Serie = @Serie,
        Numero = NuevoNumero,
        NroVenta = @Serie + '-' + CASE
            WHEN LEN(CAST(NuevoNumero AS VARCHAR(10))) >= @Longitud THEN CAST(NuevoNumero AS VARCHAR(10))
            ELSE RIGHT(REPLICATE('0', @Longitud) + CAST(NuevoNumero AS VARCHAR(10)), @Longitud)
        END;

    UPDATE dbo.Inventario_Series
    SET UltimoNumero = (
        SELECT ISNULL(MAX(Numero), 0) FROM dbo.Inventario_VentasCab WHERE Serie = @Serie
    )
    WHERE IdSerie = @IdSerie
      AND UltimoNumero < (SELECT ISNULL(MAX(Numero), 0) FROM dbo.Inventario_VentasCab WHERE Serie = @Serie);

    COMMIT TRANSACTION;
END
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.Inventario_VentasCab') AND name = 'Numero' AND is_nullable = 1
)
BEGIN
    ALTER TABLE dbo.Inventario_VentasCab ALTER COLUMN IdSerie INT NOT NULL;
    ALTER TABLE dbo.Inventario_VentasCab ALTER COLUMN Serie VARCHAR(10) NOT NULL;
    ALTER TABLE dbo.Inventario_VentasCab ALTER COLUMN Numero INT NOT NULL;
    ALTER TABLE dbo.Inventario_VentasCab ALTER COLUMN NroVenta VARCHAR(25) NOT NULL;
END
GO

IF OBJECT_ID('dbo.FK_VentasCab_Series', 'F') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab
        ADD CONSTRAINT FK_VentasCab_Series FOREIGN KEY (IdSerie)
        REFERENCES dbo.Inventario_Series (IdSerie);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'UX_VentasCab_SerieNumero'
      AND object_id = OBJECT_ID('dbo.Inventario_VentasCab')
)
    CREATE UNIQUE NONCLUSTERED INDEX UX_VentasCab_SerieNumero
        ON dbo.Inventario_VentasCab (Serie, Numero);
GO

IF OBJECT_ID('dbo.CK_Ventas_TipoComprobante', 'C') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab
        ADD CONSTRAINT CK_Ventas_TipoComprobante CHECK (
            (TipoComprobante = 'NINGUNO' AND SerieComprobante IS NULL AND NumeroComprobante IS NULL)
            OR (TipoComprobante IN ('BOLETA', 'FACTURA')
                AND SerieComprobante IS NOT NULL AND NumeroComprobante IS NOT NULL)
        );
GO
