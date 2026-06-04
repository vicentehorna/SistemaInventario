-- Consulta de referencia: historial / kárdex de movimientos por artículo (IdItem).
-- Excluye compras y ventas anuladas.

-- Ingresos por compras
SELECT
    'COMPRA' AS Tipo,
    e.RazonSocial,
    c.FechaCompra AS FechaMovimiento,
    d.PrecioUnitario,
    d.Cantidad,
    d.TotalLinea
FROM dbo.Inventario_ComprasDet d
INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = c.IdProveedor
WHERE d.IdItem = @IdItem
  AND UPPER(LTRIM(RTRIM(ISNULL(c.EstadoCompra, '')))) <> 'ANULADA'
ORDER BY c.FechaCompra, d.IdCompraDet;
GO

-- Salidas por ventas
SELECT
    'VENTA' AS Tipo,
    e.RazonSocial,
    v.FechaVenta AS FechaMovimiento,
    d.PrecioUnitario,
    d.Cantidad,
    d.TotalLinea
FROM dbo.Inventario_VentasDet d
INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = v.IdCliente
WHERE d.IdItem = @IdItem
  AND UPPER(LTRIM(RTRIM(ISNULL(v.EstadoVenta, '')))) <> 'ANULADA'
ORDER BY v.FechaVenta, d.IdVentaDet;
GO

-- Vista unificada cronológica (referencia)
SELECT *
FROM (
    SELECT
        'COMPRA' AS Tipo,
        e.RazonSocial,
        c.FechaCompra AS FechaMovimiento,
        d.PrecioUnitario,
        d.Cantidad,
        d.TotalLinea,
        d.IdCompraDet AS IdDetalle
    FROM dbo.Inventario_ComprasDet d
    INNER JOIN dbo.Inventario_ComprasCab c ON c.IdCompra = d.IdCompra
    INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = c.IdProveedor
    WHERE d.IdItem = @IdItem
      AND UPPER(LTRIM(RTRIM(ISNULL(c.EstadoCompra, '')))) <> 'ANULADA'

    UNION ALL

    SELECT
        'VENTA' AS Tipo,
        e.RazonSocial,
        v.FechaVenta AS FechaMovimiento,
        d.PrecioUnitario,
        d.Cantidad,
        d.TotalLinea,
        d.IdVentaDet AS IdDetalle
    FROM dbo.Inventario_VentasDet d
    INNER JOIN dbo.Inventario_VentasCab v ON v.IdVenta = d.IdVenta
    INNER JOIN dbo.Inventario_Empresas e ON e.IdEmpresa = v.IdCliente
    WHERE d.IdItem = @IdItem
      AND UPPER(LTRIM(RTRIM(ISNULL(v.EstadoVenta, '')))) <> 'ANULADA'
) mov
ORDER BY FechaMovimiento, Tipo, IdDetalle;
GO
