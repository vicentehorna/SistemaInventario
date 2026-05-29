-- Tablas de registro de ventas a clientes (salida de almacén)

IF OBJECT_ID('dbo.Inventario_VentasDet', 'U') IS NOT NULL
    DROP TABLE dbo.Inventario_VentasDet;
GO
IF OBJECT_ID('dbo.Inventario_VentasCab', 'U') IS NOT NULL
    DROP TABLE dbo.Inventario_VentasCab;
GO

CREATE TABLE Inventario_VentasCab (
    IdVenta INT IDENTITY(1,1) NOT NULL,
    IdCliente INT NOT NULL,
    FechaVenta DATETIME NOT NULL,
    TipoComprobante VARCHAR(20) NOT NULL,
    NroComprobanteRef VARCHAR(50) NULL,
    IncluyeIGV BIT NOT NULL CONSTRAINT DF_Ventas_IncluyeIGV DEFAULT 0,
    SubTotal DECIMAL(18,2) NOT NULL CONSTRAINT DF_Ventas_SubTotal DEFAULT 0.00,
    IGV DECIMAL(18,2) NOT NULL CONSTRAINT DF_Ventas_IGV DEFAULT 0.00,
    Total DECIMAL(18,2) NOT NULL CONSTRAINT DF_Ventas_Total DEFAULT 0.00,
    EstadoVenta VARCHAR(15) NOT NULL CONSTRAINT DF_Ventas_Estado DEFAULT 'ACTIVA',
    EstadoPago VARCHAR(15) NOT NULL CONSTRAINT DF_Ventas_EstadoPago DEFAULT 'PENDIENTE',
    FechaRegistro DATETIME CONSTRAINT DF_Ventas_FechaReg DEFAULT GETDATE(),
    CONSTRAINT PK_Inventario_VentasCab PRIMARY KEY CLUSTERED (IdVenta),
    CONSTRAINT FK_VentasCab_Empresas FOREIGN KEY (IdCliente)
        REFERENCES Inventario_Empresas (IdEmpresa)
);
GO

CREATE TABLE Inventario_VentasDet (
    IdVentaDet INT IDENTITY(1,1) NOT NULL,
    IdVenta INT NOT NULL,
    IdItem INT NOT NULL,
    Cantidad INT NOT NULL,
    PrecioUnitario DECIMAL(18,2) NOT NULL,
    TotalLinea DECIMAL(18,2) NOT NULL,
    CONSTRAINT PK_Inventario_VentasDet PRIMARY KEY CLUSTERED (IdVentaDet),
    CONSTRAINT FK_VentasDet_Cabecera FOREIGN KEY (IdVenta)
        REFERENCES Inventario_VentasCab (IdVenta) ON DELETE CASCADE,
    CONSTRAINT FK_VentasDet_Items FOREIGN KEY (IdItem)
        REFERENCES Inventario_Items (IdItem)
);
GO

CREATE NONCLUSTERED INDEX IX_Inventario_VentasCab_Cliente ON Inventario_VentasCab(IdCliente);
CREATE NONCLUSTERED INDEX IX_Inventario_VentasCab_Fecha ON Inventario_VentasCab(FechaVenta);
CREATE NONCLUSTERED INDEX IX_Inventario_VentasDet_Item ON Inventario_VentasDet(IdItem);
GO

-- Listado de ventas (filtros: código, artículo, cliente)
IF OBJECT_ID('dbo.sp_inv_lista_ventas', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_inv_lista_ventas;
GO

CREATE PROCEDURE dbo.sp_inv_lista_ventas
    @codigo VARCHAR(20),
    @articulo VARCHAR(50),
    @cliente INT
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        c.RazonSocial,
        a.FechaVenta,
        a.EstadoVenta,
        a.EstadoPago,
        d.Codigo,
        d.Descripcion,
        b.PrecioUnitario,
        b.Cantidad,
        b.TotalLinea,
        a.IdVenta
    FROM dbo.Inventario_VentasCab a
    INNER JOIN dbo.Inventario_VentasDet b ON a.IdVenta = b.IdVenta
    INNER JOIN dbo.Inventario_Empresas c ON a.IdCliente = c.IdEmpresa AND c.EsCliente = 1
    INNER JOIN dbo.Inventario_Items d ON b.IdItem = d.IdItem
    WHERE
        (@codigo = '' OR d.Codigo LIKE '%' + @codigo + '%')
        AND (@articulo = '' OR d.Descripcion LIKE '%' + @articulo + '%')
        AND (@cliente = 0 OR a.IdCliente = @cliente)
    ORDER BY a.FechaVenta;
END
GO
