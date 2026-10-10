-- Días de crédito y fecha de vencimiento en ventas (FechaVencimiento = FechaVenta + NroDias)

IF COL_LENGTH('dbo.Inventario_VentasCab', 'NroDias') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab
        ADD NroDias INT NOT NULL CONSTRAINT DF_Ventas_NroDias DEFAULT 0;
GO

IF COL_LENGTH('dbo.Inventario_VentasCab', 'FechaVencimiento') IS NULL
    ALTER TABLE dbo.Inventario_VentasCab
        ADD FechaVencimiento DATETIME NULL;
GO

UPDATE dbo.Inventario_VentasCab
SET FechaVencimiento = DATEADD(DAY, NroDias, FechaVenta)
WHERE FechaVencimiento IS NULL;
GO
