import React, { useState, useEffect } from 'react';

// Define the type for a single customer based on our Pydantic schema
interface Customer {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  phone_number: string | null;
  address: string | null;
  whatsapp_number: string | null;
  facebook_handle: string | null;
  instagram_handle: string | null;
  twitter_handle: string | null;
  created_at: string;
  updated_at: string;
}

const API_BASE_URL = '/api/v1';

const AdminDashboard = () => {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [email, setEmail] = useState('');
  const [firstName, setFirstName] = useState('');
  const [lastName, setLastName] = useState('');
  const [phone, setPhone] = useState('');
  const [address, setAddress] = useState('');
  const [whatsapp, setWhatsapp] = useState('');
  const [facebook, setFacebook] = useState('');
  const [instagram, setInstagram] = useState('');
  const [twitter, setTwitter] = useState('');

  // Brand state
  const [brands, setBrands] = useState<{ id: string; name: string; created_at: string }[]>([]);
  const [brandName, setBrandName] = useState('');

  // Product state
  const [products, setProducts] = useState<{ id: string; name: string; category: string; price: number; brand_id: string; created_at: string }[]>([]);
  const [productName, setProductName] = useState('');
  const [productCategory, setProductCategory] = useState('');
  const [productPrice, setProductPrice] = useState('');
  const [productBrandId, setProductBrandId] = useState('');

  // Promotion state
  const [promotions, setPromotions] = useState<{ id: string; name: string; description: string | null; discount_percentage: number | null; created_at: string }[]>([]);
  const [promoName, setPromoName] = useState('');
  const [promoDescription, setPromoDescription] = useState('');
  const [promoDiscount, setPromoDiscount] = useState('');

  // Cart event state
  const [eventCustomerId, setEventCustomerId] = useState('');
  const [eventCartValue, setEventCartValue] = useState('');
  const [eventMinutes, setEventMinutes] = useState('');
  const [eventResult, setEventResult] = useState<string | null>(null);

  // --- Style Objects ---
  const styleSheet = {
    container: { fontFamily: 'Arial, sans-serif', padding: '20px' },
    header: { borderBottom: '2px solid #eee', paddingBottom: '10px' },
    section: { marginTop: '30px' },
    form: { background: '#f9f9f9', padding: '15px', borderRadius: '8px', border: '1px solid #eee', display: 'flex', flexDirection: 'column' as 'column', gap: '10px', maxWidth: '400px' },
    input: { padding: '8px', borderRadius: '4px', border: '1px solid #ccc' },
    button: { padding: '10px 15px', borderRadius: '4px', border: 'none', background: '#007bff', color: 'white', cursor: 'pointer' },
    table: { width: '100%', borderCollapse: 'collapse' as 'collapse', marginTop: '20px', tableLayout: 'fixed' as 'fixed' },
    th: { borderBottom: '1px solid #ddd', padding: '12px', textAlign: 'left' as 'left', background: '#f2f2f2', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
    td: { borderBottom: '1px solid #ddd', padding: '12px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
    deleteButton: { padding: '5px 10px', borderRadius: '4px', border: 'none', background: '#dc3545', color: 'white', cursor: 'pointer' },
    error: { color: 'red', marginTop: '10px' },
  };
  
  // --- API Functions ---

  // Fetch all customers
  const fetchCustomers = async () => {
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/customers/`);
      if (!response.ok) {
        const errorData = await response.text();
        throw new Error(`HTTP error! status: ${response.status} - ${errorData}`);
      }
      const data = await response.json();
      setCustomers(data);
    } catch (e) {
      if (e instanceof Error) {
        setError(`Failed to fetch customers: ${e.message}`);
      }
    }
  };

  // Effect to fetch customers on component mount
  useEffect(() => {
    fetchCustomers();
  }, []);

  // Handle form submission to create a new customer
  const handleCreateCustomer = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/customers/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          email,
          first_name: firstName,
          last_name: lastName,
          phone_number: phone || null,
          address: address || null,
          whatsapp_number: whatsapp || null,
          facebook_handle: facebook || null,
          instagram_handle: instagram || null,
          twitter_handle: twitter || null,
        }),
      });
      if (!response.ok) {
        const errorData = await response.text();
        throw new Error(`HTTP error! status: ${response.status} - ${errorData}`);
      }
      // Reset form and refetch customers
      setEmail('');
      setFirstName('');
      setLastName('');
      setPhone('');
      setAddress('');
      setWhatsapp('');
      setFacebook('');
      setInstagram('');
      setTwitter('');
      fetchCustomers();
    } catch (e) {
       if (e instanceof Error) {
        setError(`Failed to create customer: ${e.message}`);
      }
    }
  };

  // Handle deleting a customer
  const handleDeleteCustomer = async (id: string) => {
    if (window.confirm('Are you sure you want to delete this customer?')) {
      setError(null);
      try {
        const response = await fetch(`${API_BASE_URL}/customers/${id}`, {
          method: 'DELETE',
        });
        if (!response.ok) {
          const errorData = await response.text();
          throw new Error(`HTTP error! status: ${response.status} - ${errorData}`);
        }
        fetchCustomers();
      } catch (e) {
        if (e instanceof Error) {
          setError(`Failed to delete customer: ${e.message}`);
        }
      }
    }
  };

  // --- Brands ---
  const fetchBrands = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/brands/`);
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      setBrands(await response.json());
    } catch (e) {
      if (e instanceof Error) setError(`Failed to fetch brands: ${e.message}`);
    }
  };

  useEffect(() => {
    fetchBrands();
  }, []);

  const handleCreateBrand = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/brands/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: brandName }),
      });
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status} - ${await response.text()}`);
      }
      setBrandName('');
      fetchBrands();
    } catch (e) {
      if (e instanceof Error) setError(`Failed to create brand: ${e.message}`);
    }
  };

  const handleDeleteBrand = async (id: string) => {
    if (window.confirm('Are you sure you want to delete this brand?')) {
      setError(null);
      try {
        const response = await fetch(`${API_BASE_URL}/brands/${id}`, { method: 'DELETE' });
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status} - ${await response.text()}`);
        }
        fetchBrands();
      } catch (e) {
        if (e instanceof Error) setError(`Failed to delete brand: ${e.message}`);
      }
    }
  };

  // --- Products ---
  const fetchProducts = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/products/`);
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      setProducts(await response.json());
    } catch (e) {
      if (e instanceof Error) setError(`Failed to fetch products: ${e.message}`);
    }
  };

  useEffect(() => {
    fetchProducts();
  }, []);

  const handleCreateProduct = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/products/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: productName,
          category: productCategory,
          price: parseFloat(productPrice),
          brand_id: productBrandId,
        }),
      });
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status} - ${await response.text()}`);
      }
      setProductName('');
      setProductCategory('');
      setProductPrice('');
      setProductBrandId('');
      fetchProducts();
    } catch (e) {
      if (e instanceof Error) setError(`Failed to create product: ${e.message}`);
    }
  };

  const handleDeleteProduct = async (id: string) => {
    if (window.confirm('Are you sure you want to delete this product?')) {
      setError(null);
      try {
        const response = await fetch(`${API_BASE_URL}/products/${id}`, { method: 'DELETE' });
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status} - ${await response.text()}`);
        }
        fetchProducts();
      } catch (e) {
        if (e instanceof Error) setError(`Failed to delete product: ${e.message}`);
      }
    }
  };

  // --- Promotions ---
  const fetchPromotions = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/promotions/`);
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
      setPromotions(await response.json());
    } catch (e) {
      if (e instanceof Error) setError(`Failed to fetch promotions: ${e.message}`);
    }
  };

  useEffect(() => {
    fetchPromotions();
  }, []);

  const handleCreatePromotion = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/promotions/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: promoName,
          description: promoDescription || null,
          discount_percentage: promoDiscount !== '' ? parseFloat(promoDiscount) : null,
        }),
      });
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status} - ${await response.text()}`);
      }
      setPromoName('');
      setPromoDescription('');
      setPromoDiscount('');
      fetchPromotions();
    } catch (e) {
      if (e instanceof Error) setError(`Failed to create promotion: ${e.message}`);
    }
  };

  const handleDeletePromotion = async (id: string) => {
    if (window.confirm('Are you sure you want to delete this promotion?')) {
      setError(null);
      try {
        const response = await fetch(`${API_BASE_URL}/promotions/${id}`, { method: 'DELETE' });
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status} - ${await response.text()}`);
        }
        fetchPromotions();
      } catch (e) {
        if (e instanceof Error) setError(`Failed to delete promotion: ${e.message}`);
      }
    }
  };

  // --- Cart event: tells the agent a customer left a cart, then shows what it decided ---
  const handleSendCartEvent = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setEventResult(null);
    try {
      const response = await fetch('/api/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          event_id: `admin-${Date.now()}`,
          customer_id: eventCustomerId,
          event_type: 'cart_abandoned',
          payload: {
            cart_value: parseFloat(eventCartValue),
            abandoned_minutes: parseInt(eventMinutes, 10),
          },
        }),
      });
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status} - ${await response.text()}`);
      }
      const decision = await response.json();
      setEventResult(
        decision.chosen?.channel
          ? `Nudge chosen: ${decision.chosen.channel}, offer: ${decision.chosen.offer ?? 'none'}. Message: ${decision.message ?? ''}`
          : `The agent decided to wait (no nudge now). ${(decision.blocked ?? []).slice(0, 3).join('; ')}`
      );
    } catch (e) {
      if (e instanceof Error) setError(`Failed to send cart event: ${e.message}`);
    }
  };

  return (
    <div style={styleSheet.container}>
      <h1 style={styleSheet.header}>Administrative Dashboard</h1>
      
      {error && <p style={styleSheet.error}>{error}</p>}

      {/* --- Customer Management Section --- */}
      <div style={styleSheet.section}>
        <h2>Customer Management</h2>
        
        {/* Create Customer Form */}
        <form onSubmit={handleCreateCustomer} style={styleSheet.form}>
          <h3>Add New Customer</h3>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" required style={styleSheet.input} />
          <input type="text" value={firstName} onChange={(e) => setFirstName(e.target.value)} placeholder="First Name" required style={styleSheet.input} />
          <input type="text" value={lastName} onChange={(e) => setLastName(e.target.value)} placeholder="Last Name" required style={styleSheet.input} />
          <input type="text" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="Phone (Optional)" style={styleSheet.input} />
          <input type="text" value={address} onChange={(e) => setAddress(e.target.value)} placeholder="Address (Optional)" style={styleSheet.input} />
          <input type="text" value={whatsapp} onChange={(e) => setWhatsapp(e.target.value)} placeholder="WhatsApp (Optional)" style={styleSheet.input} />
          <input type="text" value={facebook} onChange={(e) => setFacebook(e.target.value)} placeholder="Facebook (Optional)" style={styleSheet.input} />
          <input type="text" value={instagram} onChange={(e) => setInstagram(e.target.value)} placeholder="Instagram (Optional)" style={styleSheet.input} />
          <input type="text" value={twitter} onChange={(e) => setTwitter(e.target.value)} placeholder="Twitter (Optional)" style={styleSheet.input} />
          <button type="submit" style={styleSheet.button}>Add Customer</button>
        </form>

        {/* Customer List Table */}
        <table style={styleSheet.table}>
          <thead>
            <tr>
              <th style={styleSheet.th}>ID</th>
              <th style={styleSheet.th}>Email</th>
              <th style={styleSheet.th}>First Name</th>
              <th style={styleSheet.th}>Last Name</th>
              <th style={styleSheet.th}>Phone Number</th>
              <th style={styleSheet.th}>Address</th>
              <th style={styleSheet.th}>WhatsApp</th>
              <th style={styleSheet.th}>Facebook</th>
              <th style={styleSheet.th}>Instagram</th>
              <th style={styleSheet.th}>Twitter</th>
              <th style={styleSheet.th}>Created</th>
              <th style={styleSheet.th}>Updated</th>
              <th style={styleSheet.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {customers.map((customer) => (
              <tr key={customer.id}>
                <td style={styleSheet.td}>{customer.id}</td>
                <td style={styleSheet.td}>{customer.email}</td>
                <td style={styleSheet.td}>{customer.first_name}</td>
                <td style={styleSheet.td}>{customer.last_name}</td>
                <td style={styleSheet.td}>{customer.phone_number}</td>
                <td style={styleSheet.td}>{customer.address}</td>
                <td style={styleSheet.td}>{customer.whatsapp_number}</td>
                <td style={styleSheet.td}>{customer.facebook_handle}</td>
                <td style={styleSheet.td}>{customer.instagram_handle}</td>
                <td style={styleSheet.td}>{customer.twitter_handle}</td>
                <td style={styleSheet.td}>{new Date(customer.created_at).toLocaleString()}</td>
                <td style={styleSheet.td}>{new Date(customer.updated_at).toLocaleString()}</td>
                <td style={styleSheet.td}>
                  <button onClick={() => handleDeleteCustomer(customer.id)} style={styleSheet.deleteButton}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* --- Placeholder Sections --- */}
      <div style={styleSheet.section}>
        <h2>Brand Management</h2>
        <form onSubmit={handleCreateBrand} style={styleSheet.form}>
          <h3>Add New Brand</h3>
          <input type="text" value={brandName} onChange={(e) => setBrandName(e.target.value)} placeholder="Brand Name" required style={styleSheet.input} />
          <button type="submit" style={styleSheet.button}>Add Brand</button>
        </form>
        <table style={styleSheet.table}>
          <thead>
            <tr>
              <th style={styleSheet.th}>ID</th>
              <th style={styleSheet.th}>Name</th>
              <th style={styleSheet.th}>Created</th>
              <th style={styleSheet.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {brands.map((brand) => (
              <tr key={brand.id}>
                <td style={styleSheet.td}>{brand.id}</td>
                <td style={styleSheet.td}>{brand.name}</td>
                <td style={styleSheet.td}>{new Date(brand.created_at).toLocaleString()}</td>
                <td style={styleSheet.td}>
                  <button onClick={() => handleDeleteBrand(brand.id)} style={styleSheet.deleteButton}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={styleSheet.section}>
        <h2>Product Catalogue</h2>
        <form onSubmit={handleCreateProduct} style={styleSheet.form}>
          <h3>Add New Product</h3>
          <select value={productBrandId} onChange={(e) => setProductBrandId(e.target.value)} required style={styleSheet.input}>
            <option value="">Select brand</option>
            {brands.map((brand) => (
              <option key={brand.id} value={brand.id}>{brand.name}</option>
            ))}
          </select>
          <input type="text" value={productName} onChange={(e) => setProductName(e.target.value)} placeholder="Product Name" required style={styleSheet.input} />
          <input type="text" value={productCategory} onChange={(e) => setProductCategory(e.target.value)} placeholder="Category" required style={styleSheet.input} />
          <input type="number" step="0.01" min="0" value={productPrice} onChange={(e) => setProductPrice(e.target.value)} placeholder="Price" required style={styleSheet.input} />
          <button type="submit" style={styleSheet.button}>Add Product</button>
        </form>
        <table style={styleSheet.table}>
          <thead>
            <tr>
              <th style={styleSheet.th}>ID</th>
              <th style={styleSheet.th}>Name</th>
              <th style={styleSheet.th}>Category</th>
              <th style={styleSheet.th}>Price</th>
              <th style={styleSheet.th}>Brand</th>
              <th style={styleSheet.th}>Created</th>
              <th style={styleSheet.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {products.map((product) => (
              <tr key={product.id}>
                <td style={styleSheet.td}>{product.id}</td>
                <td style={styleSheet.td}>{product.name}</td>
                <td style={styleSheet.td}>{product.category}</td>
                <td style={styleSheet.td}>{product.price}</td>
                <td style={styleSheet.td}>{brands.find((b) => b.id === product.brand_id)?.name ?? product.brand_id}</td>
                <td style={styleSheet.td}>{new Date(product.created_at).toLocaleString()}</td>
                <td style={styleSheet.td}>
                  <button onClick={() => handleDeleteProduct(product.id)} style={styleSheet.deleteButton}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={styleSheet.section}>
        <h2>Promotion Management</h2>
        <form onSubmit={handleCreatePromotion} style={styleSheet.form}>
          <h3>Add New Promotion</h3>
          <input type="text" value={promoName} onChange={(e) => setPromoName(e.target.value)} placeholder="Promotion Name" required style={styleSheet.input} />
          <input type="text" value={promoDescription} onChange={(e) => setPromoDescription(e.target.value)} placeholder="Description (Optional)" style={styleSheet.input} />
          <input type="number" step="0.01" min="0" max="100" value={promoDiscount} onChange={(e) => setPromoDiscount(e.target.value)} placeholder="Discount % (Optional)" style={styleSheet.input} />
          <button type="submit" style={styleSheet.button}>Add Promotion</button>
        </form>
        <table style={styleSheet.table}>
          <thead>
            <tr>
              <th style={styleSheet.th}>ID</th>
              <th style={styleSheet.th}>Name</th>
              <th style={styleSheet.th}>Description</th>
              <th style={styleSheet.th}>Discount %</th>
              <th style={styleSheet.th}>Created</th>
              <th style={styleSheet.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {promotions.map((promo) => (
              <tr key={promo.id}>
                <td style={styleSheet.td}>{promo.id}</td>
                <td style={styleSheet.td}>{promo.name}</td>
                <td style={styleSheet.td}>{promo.description}</td>
                <td style={styleSheet.td}>{promo.discount_percentage}</td>
                <td style={styleSheet.td}>{new Date(promo.created_at).toLocaleString()}</td>
                <td style={styleSheet.td}>
                  <button onClick={() => handleDeletePromotion(promo.id)} style={styleSheet.deleteButton}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={styleSheet.section}>
        <h2>Cart Events (input for the agent)</h2>
        <form onSubmit={handleSendCartEvent} style={styleSheet.form}>
          <h3>Send Cart Abandoned Event</h3>
          <select value={eventCustomerId} onChange={(e) => setEventCustomerId(e.target.value)} required style={styleSheet.input}>
            <option value="">Select customer</option>
            {customers.map((c) => (
              <option key={c.id} value={c.id}>{c.first_name} {c.last_name} ({c.email})</option>
            ))}
          </select>
          <input type="number" step="0.01" min="0" value={eventCartValue} onChange={(e) => setEventCartValue(e.target.value)} placeholder="Cart value" required style={styleSheet.input} />
          <input type="number" step="1" min="0" value={eventMinutes} onChange={(e) => setEventMinutes(e.target.value)} placeholder="Minutes since the cart was abandoned" required style={styleSheet.input} />
          <button type="submit" style={styleSheet.button}>Send Event</button>
        </form>
        {eventResult && <p>{eventResult}</p>}
      </div>
    </div>
  );
};

export default AdminDashboard;