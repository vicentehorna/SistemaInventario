-- Índices recomendados para acelerar el kárdex por IdItem (ejecutar una vez en hm_safari).

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_Inventario_ComprasDet_Item_IdCompra'
      AND object_id = OBJECT_ID('dbo.Inventario_ComprasDet')
)
    CREATE NONCLUSTERED INDEX IX_Inventario_ComprasDet_Item_IdCompra
        ON dbo.Inventario_ComprasDet (IdItem, IdCompra)
        INCLUDE (Cantidad, PrecioUnitario, TotalLinea);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_Inventario_VentasDet_Item_IdVenta'
      AND object_id = OBJECT_ID('dbo.Inventario_VentasDet')
)
    CREATE NONCLUSTERED INDEX IX_Inventario_VentasDet_Item_IdVenta
        ON dbo.Inventario_VentasDet (IdItem, IdVenta)
        INCLUDE (Cantidad, PrecioUnitario, TotalLinea);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_Inventario_ComprasCab_Estado'
      AND object_id = OBJECT_ID('dbo.Inventario_ComprasCab')
)
    CREATE NONCLUSTERED INDEX IX_Inventario_ComprasCab_Estado
        ON dbo.Inventario_ComprasCab (EstadoCompra, IdCompra);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_Inventario_VentasCab_Estado'
      AND object_id = OBJECT_ID('dbo.Inventario_VentasCab')
)
    CREATE NONCLUSTERED INDEX IX_Inventario_VentasCab_Estado
        ON dbo.Inventario_VentasCab (EstadoVenta, IdVenta);
GO
