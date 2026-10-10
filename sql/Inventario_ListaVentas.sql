-- Listado de ventas (incluye FechaVencimiento). Requiere sql/Inventario_Ventas_Credito.sql

ALTER PROCEDURE [dbo].[sp_inv_lista_ventas]
    @codigo varchar(20),
    @articulo varchar(50),
    @cliente int
AS
BEGIN
    SELECT
        c.razonsocial, a.FechaVenta, a.EstadoVenta, a.EstadoPago, d.codigo, d.descripcion,
        b.preciounitario, b.cantidad, b.totallinea, a.IdVenta, a.FechaVencimiento
    FROM Inventario_VentasCab a
    INNER JOIN Inventario_VentasDet b ON (a.IdVenta = b.IdVenta)
    INNER JOIN inventario_empresas c ON (a.IdCliente = c.idempresa AND escliente = 1)
    INNER JOIN Inventario_items d ON (b.IdItem = d.iditem)
    WHERE
        (@codigo = '' OR d.codigo LIKE '%' + @codigo + '%') AND
        (@articulo = '' OR d.descripcion LIKE '%' + @articulo + '%') AND
        (@cliente = 0 OR a.IdCliente = @cliente)
    ORDER BY 2
END
GO
