-- Días de crédito y fecha de vencimiento en compras (FechaVencimiento = FechaCompra + NroDias)

IF COL_LENGTH('dbo.Inventario_ComprasCab', 'NroDias') IS NULL
    ALTER TABLE dbo.Inventario_ComprasCab
        ADD NroDias INT NOT NULL CONSTRAINT DF_Compras_NroDias DEFAULT 0;
GO

IF COL_LENGTH('dbo.Inventario_ComprasCab', 'FechaVencimiento') IS NULL
    ALTER TABLE dbo.Inventario_ComprasCab
        ADD FechaVencimiento DATETIME NULL;
GO

UPDATE dbo.Inventario_ComprasCab
SET FechaVencimiento = DATEADD(DAY, NroDias, FechaCompra)
WHERE FechaVencimiento IS NULL;
GO
