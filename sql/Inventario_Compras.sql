-- Tablas de registro de compras a proveedores (ingreso a almacén)

IF OBJECT_ID('dbo.Inventario_ComprasDet', 'U') IS NOT NULL
    DROP TABLE dbo.Inventario_ComprasDet;
GO
IF OBJECT_ID('dbo.Inventario_ComprasCab', 'U') IS NOT NULL
    DROP TABLE dbo.Inventario_ComprasCab;
GO

CREATE TABLE Inventario_ComprasCab (
    IdCompra INT IDENTITY(1,1) NOT NULL,
    IdProveedor INT NOT NULL,
    FechaCompra DATETIME NOT NULL,
    TipoComprobante VARCHAR(20) NOT NULL,
    NroComprobanteRef VARCHAR(50) NULL,
    IncluyeIGV BIT NOT NULL CONSTRAINT DF_Compras_IncluyeIGV DEFAULT 0,
    SubTotal DECIMAL(18,2) NOT NULL CONSTRAINT DF_Compras_SubTotal DEFAULT 0.00,
    IGV DECIMAL(18,2) NOT NULL CONSTRAINT DF_Compras_IGV DEFAULT 0.00,
    Total DECIMAL(18,2) NOT NULL CONSTRAINT DF_Compras_Total DEFAULT 0.00,
    EstadoCompra VARCHAR(15) NOT NULL CONSTRAINT DF_Compras_Estado DEFAULT 'ACTIVA',
    EstadoPago VARCHAR(15) NOT NULL CONSTRAINT DF_Compras_EstadoPago DEFAULT 'PENDIENTE',
    FechaRegistro DATETIME CONSTRAINT DF_Compras_FechaReg DEFAULT GETDATE(),
    CONSTRAINT PK_Inventario_ComprasCab PRIMARY KEY CLUSTERED (IdCompra),
    CONSTRAINT FK_ComprasCab_Empresas FOREIGN KEY (IdProveedor)
        REFERENCES Inventario_Empresas (IdEmpresa)
);
GO

CREATE TABLE Inventario_ComprasDet (
    IdCompraDet INT IDENTITY(1,1) NOT NULL,
    IdCompra INT NOT NULL,
    IdItem INT NOT NULL,
    Cantidad INT NOT NULL,
    PrecioUnitario DECIMAL(18,2) NOT NULL,
    TotalLinea DECIMAL(18,2) NOT NULL,
    CONSTRAINT PK_Inventario_ComprasDet PRIMARY KEY CLUSTERED (IdCompraDet),
    CONSTRAINT FK_ComprasDet_Cabecera FOREIGN KEY (IdCompra)
        REFERENCES Inventario_ComprasCab (IdCompra) ON DELETE CASCADE,
    CONSTRAINT FK_ComprasDet_Items FOREIGN KEY (IdItem)
        REFERENCES Inventario_Items (IdItem)
);
GO

CREATE NONCLUSTERED INDEX IX_Inventario_ComprasCab_Proveedor ON Inventario_ComprasCab(IdProveedor);
CREATE NONCLUSTERED INDEX IX_Inventario_ComprasCab_Fecha ON Inventario_ComprasCab(FechaCompra);
CREATE NONCLUSTERED INDEX IX_Inventario_ComprasDet_Item ON Inventario_ComprasDet(IdItem);
GO
