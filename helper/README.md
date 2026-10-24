# Ajudante do gerador de links (Tampermonkey)

Script que roda **no seu navegador**, na página do gerador de links do Mercado Livre, e tira a colagem
manual do meio. Quem clica em **Gerar** continua sendo você (o ML protege a criação do link com reCAPTCHA;
o script não toca nisso e não envia a sua sessão do ML a ninguém).

## Instalar

1. Instale a extensão [Tampermonkey](https://www.tampermonkey.net/) no navegador onde você está logado no ML.
2. Tampermonkey > Criar novo script > apague o modelo, cole o conteúdo de `achadosbjj-links.user.js` e salve.
3. Menu do Tampermonkey > **achadosbjj: configurar painel e token**: informe o endereço do painel e o
   `ADMIN_TOKEN`. O endereço pode ser:
   - `http://localhost:8001`, se você abre o painel por túnel SSH (`ssh -L 8001:127.0.0.1:8001 usuario@vps`);
   - `https://achados.tibiascope.com`, se publicou o subdomínio (ver `deploy/README.md`).
   Se usar outro endereço, acrescente uma linha `// @connect <host>` no cabeçalho do script.

## Usar (uns 20 segundos por lote)

1. Abra `https://www.mercadolivre.com.br/afiliados/linkbuilder` e, no cantinho, clique em **Buscar lote**:
   as URLs das ofertas da fila (até 20, da melhor para a pior) são coladas no campo.
2. Clique em **Gerar** (deixe em **Links curtos**).
3. Quando os links aparecerem, o ajudante mostra a tabela produto ↔ link. Confira e clique em **Salvar N links**.
   As ofertas ficam prontas para enviar no painel.

## Quando ele para

- O número de links curtos na página não bate com o do lote (o pareamento é pela ordem, então qualquer
  diferença é descartada): aparece o aviso e **nenhum botão de salvar**.
- O servidor recusa o lote (link repetido, de outro produto, produto sumido…): nada é salvo.
- O ML pede verificação ou muda a página: o script não acha o campo ou os links e não faz nada. A tela
  **Gerar links** do painel continua funcionando.

## Testes

```bash
node --test helper/achadosbjj-links.test.js
```
