-- Listado de compras (incluye FechaVencimiento). Requiere sql/Inventario_Compras_Credito.sql

ALTER PROCEDURE [dbo].[sp_inv_lista_compras]
    @codigo varchar(20),
    @articulo varchar(50),
    @proveedor int,
    @estadopago char(20)
AS
BEGIN
    SELECT
        c.razonsocial, a.FechaCompra, a.EstadoCompra, a.EstadoPago, d.codigo, d.descripcion,
        b.preciounitario, b.cantidad, b.totallinea, a.IdCompra, a.FechaVencimiento
    FROM Inventario_ComprasCab a
    INNER JOIN Inventario_ComprasDet b ON (a.IdCompra = b.IdCompra)
    INNER JOIN inventario_empresas c ON (a.IdProveedor = c.idempresa AND esproveedor = 1)
    INNER JOIN Inventario_items d ON (b.IdItem = d.iditem)
    WHERE
        (@codigo = '' OR d.codigo LIKE '%' + @codigo + '%') AND
        (@articulo = '' OR d.descripcion LIKE '%' + @articulo + '%') AND
        (@proveedor = 0 OR a.IdProveedor = @proveedor) AND
        (@estadopago = '' OR a.estadopago = @estadopago)
    ORDER BY 2
END
GO
