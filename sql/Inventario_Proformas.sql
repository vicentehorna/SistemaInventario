-- Proformas (cotizaciones comerciales, sin impacto en stock ni IGV obligatorio)

IF OBJECT_ID('dbo.Inventario_ProformasCab', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Inventario_ProformasCab (
        IdProforma INT IDENTITY(1,1) NOT NULL,
        NroProforma VARCHAR(20) NOT NULL,
        IdCliente INT NOT NULL,
        FechaProforma DATETIME NOT NULL,
        Total DECIMAL(18,2) NOT NULL CONSTRAINT DF_Proformas_Total DEFAULT 0.00,
        FechaRegistro DATETIME CONSTRAINT DF_Proformas_FechaReg DEFAULT GETDATE(),
        CONSTRAINT PK_Inventario_ProformasCab PRIMARY KEY CLUSTERED (IdProforma),
        CONSTRAINT UQ_Proformas_Nro UNIQUE (NroProforma),
        CONSTRAINT FK_ProformasCab_Empresas FOREIGN KEY (IdCliente)
            REFERENCES dbo.Inventario_Empresas (IdEmpresa)
    );
END
GO

IF OBJECT_ID('dbo.Inventario_ProformasDet', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.Inventario_ProformasDet (
        IdProformaDet INT IDENTITY(1,1) NOT NULL,
        IdProforma INT NOT NULL,
        IdItem INT NOT NULL,
        Cantidad INT NOT NULL,
        PrecioUnitario DECIMAL(18,2) NOT NULL,
        TotalLinea DECIMAL(18,2) NOT NULL,
        CONSTRAINT PK_Inventario_ProformasDet PRIMARY KEY CLUSTERED (IdProformaDet),
        CONSTRAINT FK_ProformasDet_Cabecera FOREIGN KEY (IdProforma)
            REFERENCES dbo.Inventario_ProformasCab (IdProforma) ON DELETE CASCADE,
        CONSTRAINT FK_ProformasDet_Items FOREIGN KEY (IdItem)
            REFERENCES dbo.Inventario_Items (IdItem)
    );
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_Inventario_ProformasCab_Cliente'
      AND object_id = OBJECT_ID('dbo.Inventario_ProformasCab')
)
    CREATE NONCLUSTERED INDEX IX_Inventario_ProformasCab_Cliente
        ON dbo.Inventario_ProformasCab (IdCliente);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_Inventario_ProformasCab_Fecha'
      AND object_id = OBJECT_ID('dbo.Inventario_ProformasCab')
)
    CREATE NONCLUSTERED INDEX IX_Inventario_ProformasCab_Fecha
        ON dbo.Inventario_ProformasCab (FechaProforma);
GO

IF OBJECT_ID('dbo.sp_inv_lista_proformas', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_inv_lista_proformas;
GO

CREATE PROCEDURE dbo.sp_inv_lista_proformas
    @codigo VARCHAR(20),
    @articulo VARCHAR(50),
    @cliente INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        c.RazonSocial,
        a.FechaProforma,
        d.Codigo,
        d.Descripcion,
        b.PrecioUnitario,
        b.Cantidad,
        b.TotalLinea,
        a.IdProforma
    FROM dbo.Inventario_ProformasCab a
    INNER JOIN dbo.Inventario_ProformasDet b ON a.IdProforma = b.IdProforma
    INNER JOIN dbo.Inventario_Empresas c ON a.IdCliente = c.IdEmpresa AND c.EsCliente = 1
    INNER JOIN dbo.Inventario_Items d ON b.IdItem = d.IdItem
    WHERE
        (@codigo = '' OR d.Codigo LIKE '%' + @codigo + '%')
        AND (@articulo = '' OR d.Descripcion LIKE '%' + @articulo + '%')
        AND (@cliente = 0 OR a.IdCliente = @cliente)
    ORDER BY a.FechaProforma;
END
GO
