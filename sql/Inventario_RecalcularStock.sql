-- Recalcula StockActual en Inventario_Items a partir de movimientos reales.
-- Fórmula por artículo:
--   StockActual = SUM(cantidades en compras NO anuladas) - SUM(cantidades en ventas NO anuladas)
--
-- No considera proformas ni el stock inicial registrado al crear el artículo.
-- Artículos sin compras ni ventas quedarán con StockActual = 0.
--
-- Ejecutar en la base de datos del inventario (revisar la vista previa antes del UPDATE).

SET NOCOUNT ON;
GO

-- ---------------------------------------------------------------------------
-- 1) Vista previa: stock actual vs stock calculado
-- ---------------------------------------------------------------------------
;WITH Compras AS (
    SELECT
        d.IdItem,
        SUM(d.Cantidad) AS TotalCompras
    FROM dbo.Inventario_ComprasDet d
    INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
    WHERE UPPER(LTRIM(RTRIM(ISNULL(c.EstadoCompra, '')))) <> 'ANULADA'
    GROUP BY d.IdItem
),
Ventas AS (
    SELECT
        d.IdItem,
        SUM(d.Cantidad) AS TotalVentas
    FROM dbo.Inventario_VentasDet d
    INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
    WHERE UPPER(LTRIM(RTRIM(ISNULL(v.EstadoVenta, '')))) <> 'ANULADA'
    GROUP BY d.IdItem
),
StockCalculado AS (
    SELECT
        i.IdItem,
        i.Codigo,
        i.Descripcion,
        i.StockActual AS StockActualAnterior,
        ISNULL(c.TotalCompras, 0) AS TotalCompras,
        ISNULL(v.TotalVentas, 0) AS TotalVentas,
        ISNULL(c.TotalCompras, 0) - ISNULL(v.TotalVentas, 0) AS StockCalculado
    FROM dbo.Inventario_Items i
    LEFT JOIN Compras c ON c.IdItem = i.IdItem
    LEFT JOIN Ventas v ON v.IdItem = i.IdItem
)
SELECT
    IdItem,
    Codigo,
    Descripcion,
    StockActualAnterior,
    TotalCompras,
    TotalVentas,
    StockCalculado,
    StockCalculado - StockActualAnterior AS Diferencia
FROM StockCalculado
WHERE StockCalculado <> StockActualAnterior
   OR TotalCompras > 0
   OR TotalVentas > 0
ORDER BY Codigo;
GO

-- ---------------------------------------------------------------------------
-- 2) Actualización de StockActual
-- ---------------------------------------------------------------------------
BEGIN TRANSACTION;

BEGIN TRY
    ;WITH Compras AS (
        SELECT
            d.IdItem,
            SUM(d.Cantidad) AS TotalCompras
        FROM dbo.Inventario_ComprasDet d
        INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
        WHERE UPPER(LTRIM(RTRIM(ISNULL(c.EstadoCompra, '')))) <> 'ANULADA'
        GROUP BY d.IdItem
    ),
    Ventas AS (
        SELECT
            d.IdItem,
            SUM(d.Cantidad) AS TotalVentas
        FROM dbo.Inventario_VentasDet d
        INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
        WHERE UPPER(LTRIM(RTRIM(ISNULL(v.EstadoVenta, '')))) <> 'ANULADA'
        GROUP BY d.IdItem
    ),
    StockCalculado AS (
        SELECT
            i.IdItem,
            ISNULL(c.TotalCompras, 0) - ISNULL(v.TotalVentas, 0) AS StockNuevo
        FROM dbo.Inventario_Items i
        LEFT JOIN Compras c ON c.IdItem = i.IdItem
        LEFT JOIN Ventas v ON v.IdItem = i.IdItem
    )
    UPDATE i
    SET i.StockActual = sc.StockNuevo
    FROM dbo.Inventario_Items i
    INNER JOIN StockCalculado sc ON sc.IdItem = i.IdItem;

    COMMIT TRANSACTION;

    PRINT 'Stock recalculado correctamente.';
END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0
        ROLLBACK TRANSACTION;

    DECLARE @ErrorMsg NVARCHAR(4000) = ERROR_MESSAGE();
    RAISERROR('Error al recalcular stock: %s', 16, 1, @ErrorMsg);
END CATCH;
GO

-- ---------------------------------------------------------------------------
-- 3) Verificación: artículos con stock negativo (posible inconsistencia histórica)
-- ---------------------------------------------------------------------------
SELECT
    i.IdItem,
    i.Codigo,
    i.Descripcion,
    i.StockActual
FROM dbo.Inventario_Items i
WHERE i.StockActual < 0
ORDER BY i.Codigo;
GO
