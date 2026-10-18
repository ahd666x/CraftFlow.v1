from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from product.models import Product, ProductCategory


class StaticViewSitemap(Sitemap):
    priority = 0.5
    changefreq = 'daily'

    def items(self):
        return [
            'storefront:home',
            'storefront:product_list',
            'storefront:category_list',
            'storefront:about',
            'accounts:login',
            'accounts:register',
        ]

    def location(self, item):
        return reverse(item)


class ProductSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.8

    def items(self):
        return Product.active_objects.all()

    def location(self, obj):
        return reverse('storefront:product_detail', kwargs={'slug': obj.slug})


class CategorySitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.7

    def items(self):
        return ProductCategory.objects.filter(is_active=True)

    def location(self, obj):
        return reverse('storefront:category_detail', kwargs={'slug': obj.slug})
